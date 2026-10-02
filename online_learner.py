"""
online_learner.py — pure-python online logistic regression that learns, from the
bot's own confirmed trades, P(this Scout trade wins).

  * Features are snapshotted at Scout ENTRY (regime_gate.features() + symbol /
    strategy recent win-rates + position in the 3m45s leg + hour of day).
  * It learns from EVERY confirmed Scout result (gate open or not): one SGD step
    per result, using the features that were known at entry (no leakage).
  * Every prediction is scored BEFORE the update (prequential), so `useful()` is an
    honest out-of-sample test: log-loss better than "just predict the running win
    rate" AND top-half predictions winning more than bottom-half.
  * Follower use: `allow(cid)` vetoes an entry only when the model is trained AND
    useful() AND p < breakeven + margin. Otherwise it passes everything through, so
    an untrained / useless model can never hurt.

State is tiny (~16 weights + two small dicts + 300 scored predictions) and is
persisted in the same Render env-var batch as the gate (SF_ML_STATE).
"""
import logging
import math
import time
from collections import deque
from typing import Callable, Dict, Optional

import config

logger = logging.getLogger("online_learner")

FEATURES = ["wr3", "wr6", "wr12", "wr25", "loss_run", "win_run", "slope6", "dd25",
            "gate_open", "leg_age", "hour_sin", "hour_cos", "sym_wr", "strat_wr", "is_differ",
            "belief", "edge", "sep", "gap"]


def _cfg(name, default):
    return getattr(config, name, default)


def _sig(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _logit(p: float) -> float:
    p = min(0.999, max(0.001, p))
    return math.log(p / (1 - p))


class OnlineLearner:
    def __init__(self, gate, clock: Callable[[], float] = time.time):
        self.gate = gate
        self._clock = clock
        self.boot_ts = clock()
        self.w = [0.0] * len(FEATURES)
        self.b = _logit(1.0 / float(_cfg("EDGE_PAYOUT_MULTIPLE", 2.12)))
        self.n = 0                       # results learned from
        self.wr_all = 0.47               # running win rate (the baseline to beat)
        self.sym: Dict[str, float] = {}
        self.strat: Dict[str, float] = {}
        self.pending: Dict[str, tuple] = {}     # cid -> (x, p, p0)
        self.scored: deque = deque(maxlen=500)  # [p, y, p0]
        self.vetoed = 0
        self.passed = 0

    # ── features ────────────────────────────────────────────────────────
    def _x(self, ev: dict) -> list:
        f = self.gate.features()
        leg = float(_cfg("REDEPLOY_INTERVAL_SECS", 225)) or 225.0
        age = min(1.0, max(0.0, (self._clock() - self.boot_ts) / leg))
        hr = (time.gmtime(self._clock()).tm_hour + time.gmtime(self._clock()).tm_min / 60.0) / 24.0
        sym = str(ev.get("symbol", ""))
        st = str(ev.get("strategy", ""))
        return [
            (f["wr3"] - 0.5) * 2, (f["wr6"] - 0.5) * 2, (f["wr12"] - 0.5) * 2,
            (f["wr25"] - 0.5) * 2, min(f["loss_run"], 6) / 6.0, min(f["win_run"], 6) / 6.0,
            max(-1.0, min(1.0, f["slope6"])), min(f["dd25"], 6.0) / 6.0, f["open"],
            age * 2 - 1, math.sin(2 * math.pi * hr), math.cos(2 * math.pi * hr),
            (self.sym.get(sym, 0.47) - 0.5) * 2, (self.strat.get(st, 0.47) - 0.5) * 2,
            1.0 if "DIFF" in str(ev.get("contract_type", "")).upper() else 0.0,
            # HMM engine (0 when the rules engine is active)
            (f.get("belief", 0.5) - 0.5) * 2, max(-1.0, min(1.0, f.get("edge", 0.0) * 5.0)),
            min(1.0, max(0.0, f.get("sep", 0.0) * 2.0)),
            min(1.0, math.log1p(max(0.0, f.get("gap", 0.0))) / math.log1p(300.0)) * 2 - 1]

    def predict_x(self, x: list) -> float:
        return _sig(self.b + sum(wi * xi for wi, xi in zip(self.w, x)))

    # ── bus handlers ────────────────────────────────────────────────────
    def on_entry(self, ev: dict) -> None:
        cid = str(ev.get("contract_id", "") or "")
        if not cid:
            return
        x = self._x(ev)
        self.pending[cid] = (x, self.predict_x(x), self.wr_all)
        if len(self.pending) > 400:
            self.pending.pop(next(iter(self.pending)))

    def on_result(self, ev: dict) -> None:
        if not ev.get("confirmed", True) or ev.get("gate_eligible", True) is False:
            return
        try:
            pnl = float(ev.get("pnl", 0) or 0)
        except (TypeError, ValueError):
            return
        if pnl == 0:
            return
        y = 1.0 if pnl > 0 else 0.0
        cid = str(ev.get("contract_id", "") or "")
        rec = self.pending.pop(cid, None)
        sym = str(ev.get("symbol", "")); st = str(ev.get("strategy", ""))
        a = float(_cfg("ML_ENTITY_ALPHA", 0.08))
        self.sym[sym] = self.sym.get(sym, 0.47) * (1 - a) + y * a
        self.strat[st] = self.strat.get(st, 0.47) * (1 - a) + y * a
        if len(self.sym) > 40:
            self.sym.pop(next(iter(self.sym)))
        if rec is not None:
            x, p, p0 = rec
            self.scored.append([round(p, 4), int(y), round(p0, 4)])   # scored BEFORE learning
            lr = max(float(_cfg("ML_LR_MIN", 0.01)),
                     float(_cfg("ML_LR", 0.05)) / math.sqrt(1.0 + self.n / 200.0))
            l2 = float(_cfg("ML_L2", 0.002))
            g = p - y
            for i, xi in enumerate(x):
                self.w[i] -= lr * (g * xi + l2 * self.w[i])
            self.b -= lr * g
            self.n += 1
        ab = 1.0 / min(200.0, self.n + 20.0)
        self.wr_all += (y - self.wr_all) * ab

    # ── honesty metrics ─────────────────────────────────────────────────
    def metrics(self) -> dict:
        s = list(self.scored)
        m = len(s)
        out = {"n_learned": self.n, "n_scored": m, "logloss": None, "baseline_logloss": None,
               "lift": None, "useful": False}
        if m < 20:
            return out
        def ll(p, y):
            p = min(0.999, max(0.001, p))
            return -(y * math.log(p) + (1 - y) * math.log(1 - p))
        out["logloss"] = sum(ll(p, y) for p, y, _ in s) / m
        out["baseline_logloss"] = sum(ll(p0, y) for _, y, p0 in s) / m
        srt = sorted(s, key=lambda r: r[0])
        lo, hi = srt[: m // 2], srt[m // 2:]
        wr = lambda xs: sum(r[1] for r in xs) / len(xs)
        out["lift"] = wr(hi) - wr(lo)
        out["useful"] = bool(m >= int(_cfg("ML_USEFUL_MIN_SCORED", 100))
                             and out["logloss"] < out["baseline_logloss"] - float(_cfg("ML_MIN_LOGLOSS_GAIN", 0.002))
                             and out["lift"] >= float(_cfg("ML_MIN_LIFT", 0.03)))
        return out

    def trained(self) -> bool:
        return self.n >= int(_cfg("ML_MIN_SAMPLES", 150))

    # ── follower hook ───────────────────────────────────────────────────
    def p_for(self, cid: str) -> Optional[float]:
        rec = self.pending.get(str(cid))
        return rec[1] if rec else None

    def allow(self, cid: str):
        """(ok, why). Never blocks unless trained AND useful AND the model says p < breakeven+margin."""
        if not bool(_cfg("ML_FILTER_ENABLED", True)):
            return True, "ml filter off"
        p = self.p_for(cid)
        if p is None or not self.trained():
            self.passed += 1
            return True, "ml not trained yet"
        if not self.metrics()["useful"]:
            self.passed += 1
            return True, "ml not (yet) better than baseline"
        need = self.gate.breakeven() + float(_cfg("ML_FILTER_MARGIN", 0.0))
        if p < need:
            self.vetoed += 1
            return False, f"ml p={p:.2f} < {need:.2f}"
        self.passed += 1
        return True, f"ml p={p:.2f}"

    # ── persistence / reporting ─────────────────────────────────────────
    def export(self) -> dict:
        return {"w": [round(x, 5) for x in self.w], "b": round(self.b, 5), "n": self.n,
                "wr": round(self.wr_all, 4),
                "sym": {k: round(v, 3) for k, v in self.sym.items()},
                "strat": {k: round(v, 3) for k, v in self.strat.items()},
                "sc": list(self.scored), "veto": self.vetoed, "pass": self.passed}

    def load(self, d: dict) -> None:
        try:
            w = d.get("w") or []
            if len(w) != len(FEATURES):
                # feature set changed (upgrade): old weights/scores are not comparable -> restart clean
                logger.info(f"ML: feature set changed ({len(w)} -> {len(FEATURES)}); learner restarts")
                self.sym = {str(k): float(v) for k, v in (d.get("sym") or {}).items()}
                self.strat = {str(k): float(v) for k, v in (d.get("strat") or {}).items()}
                return
            self.w = [float(x) for x in w]
            self.b = float(d.get("b", self.b)); self.n = int(d.get("n", 0))
            self.wr_all = float(d.get("wr", self.wr_all))
            self.sym = {str(k): float(v) for k, v in (d.get("sym") or {}).items()}
            self.strat = {str(k): float(v) for k, v in (d.get("strat") or {}).items()}
            self.scored.extend(d.get("sc") or [])
            self.vetoed = int(d.get("veto", 0)); self.passed = int(d.get("pass", 0))
            logger.info(f"ML: restored learner (n={self.n}, scored={len(self.scored)})")
        except Exception as exc:
            logger.warning(f"ML: could not restore learner: {exc}")

    def state(self) -> dict:
        m = self.metrics()
        top = sorted(zip(FEATURES, self.w), key=lambda kv: -abs(kv[1]))[:4]
        return {**m, "trained": self.trained(), "vetoed": self.vetoed, "passed": self.passed,
                "baseline_wr": round(self.wr_all, 3),
                "top_features": [[k, round(v, 2)] for k, v in top]}

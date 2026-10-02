"""
edge_gate.py — rule-based "does the Scout currently have an edge?" gate.
NO ML, NO training. Pure statistics, fully explainable.

Evidence: the last N CONFIRMED settled Scout results (config.EDGE_GATE_WINDOW).
Estimate : Beta posterior of the true win rate, prior centred on break-even.
           Optional time decay (config.EDGE_GATE_DECAY_HALFLIFE trades).
Statistic: P(true win rate > break-even + margin).

  OPEN   when P >= EDGE_GATE_OPEN_PROB  and n >= EDGE_GATE_MIN_TRADES
  CLOSE  when P <  EDGE_GATE_CLOSE_PROB, or the last K results are all losses,
         or force_close() is called (e.g. a Scout guard event).
Hysteresis: different open/close thresholds + a minimum open and minimum
closed duration. Safety closes (loss run / forced) ignore the minimum-open
time; the soft "P fell" close respects it.

Break-even = mean of 1/(payout/stake) over the window, using the real payout
per trade where known, else 1/config.EDGE_PAYOUT_MULTIPLE (2.12 -> 47.17 %).

Interface: on_result(event), is_open(), state(), force_close(reason),
           export() / load() for restart persistence.
"""
import logging
import math
import time
from collections import deque
from typing import Callable, Deque, Optional

import config

logger = logging.getLogger("edge_gate")


# ── regularised incomplete beta (no scipy) ───────────────────────────────────
def _betacf(a: float, b: float, x: float) -> float:
    tiny, qab, qap, qam = 1e-30, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = tiny if abs(d) < tiny else d
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d; d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c; c = tiny if abs(c) < tiny else c
        d = 1.0 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d; d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c; c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def prob_above(wins: float, losses: float, threshold: float,
               prior_a: float = 1.0, prior_b: float = 1.0) -> float:
    """P(true win rate > threshold) under Beta(prior_a+wins, prior_b+losses)."""
    return 1.0 - betainc(prior_a + wins, prior_b + losses, threshold)


def _cfg(name: str, default):
    return getattr(config, name, default)


class EdgeGate:
    def __init__(self, clock: Callable[[], float] = time.time, **over):
        self._clock = clock
        g = lambda k, d: over.get(k, _cfg("EDGE_GATE_" + k.upper(), d))
        self.window       = int(g("window", 30))
        self.min_trades   = int(g("min_trades", 10))
        self.margin       = float(g("margin", 0.02))
        self.open_prob    = float(g("open_prob", 0.90))
        self.close_prob   = float(g("close_prob", 0.60))
        self.loss_run     = int(g("close_loss_run", 5))
        self.min_open     = float(g("min_open_secs", 180))
        self.min_closed   = float(g("min_closed_secs", 180))
        self.halflife     = float(g("decay_halflife", 0))
        self.prior_n      = float(g("prior_strength", 2.0))
        self.stale_secs   = float(over.get("stale_secs", _cfg("EDGE_GATE_STALE_SECS", 900)))
        self.default_payout = float(over.get("payout", _cfg("EDGE_PAYOUT_MULTIPLE", 2.12)))

        # window items: (won, payout_ratio_or_0, ts)
        self._win: Deque[tuple] = deque(maxlen=self.window)
        self._seen: Deque[str] = deque(maxlen=60)
        self._open = False
        self._since = self._clock()
        self._reason = "initial"
        self._last_result_ts = 0.0
        self.cycles = 0                 # number of closed->open transitions
        self.ignored_unconfirmed = 0
        self.history: list = []         # [(ts, "OPEN"/"CLOSED", reason)] (last 50)

    # ── evidence ────────────────────────────────────────────────────────
    def on_result(self, ev: dict) -> None:
        if not ev.get("confirmed", True):
            self.ignored_unconfirmed += 1
            logger.warning(f"EDGE GATE: ignoring unconfirmed result {ev.get('contract_id', '?')}")
            return
        if ev.get("gate_eligible", True) is False:
            return
        cid = str(ev.get("contract_id", "") or "")
        if cid:
            if cid in self._seen:
                return
            self._seen.append(cid)
        try:
            pnl = float(ev.get("pnl", 0.0))
        except (TypeError, ValueError):
            return
        if pnl == 0:
            return                      # neither a win nor a loss
        stake = float(ev.get("stake", 0) or 0)
        payout = float(ev.get("payout", 0) or 0)
        ratio = payout / stake if stake > 0 and payout > stake else 0.0
        now = float(ev.get("ts", 0) or 0) or self._clock()
        self._win.append((pnl > 0, ratio, now))
        self._last_result_ts = self._clock()
        self._evaluate(forced_reason=None)

    def force_close(self, reason: str) -> None:
        if self._open:
            self._set(False, f"forced: {reason}", self._numbers())

    # ── maths ───────────────────────────────────────────────────────────
    def breakeven(self) -> float:
        inv = [1.0 / r for (_, r, _) in self._win if r > 1.0]
        if inv:
            return sum(inv) / len(inv)
        return 1.0 / self.default_payout

    def _counts(self):
        n = len(self._win)
        w = l = 0.0
        for i, (won, _, _) in enumerate(self._win):
            wt = 0.5 ** ((n - 1 - i) / self.halflife) if self.halflife > 0 else 1.0
            if won: w += wt
            else:   l += wt
        return w, l

    def _numbers(self) -> dict:
        w, l = self._counts()
        be = self.breakeven()
        thr = min(0.999, be + self.margin)
        p = prob_above(w, l, thr, self.prior_n * be, self.prior_n * (1 - be))
        n = len(self._win)
        raw_w = sum(1 for x in self._win if x[0])
        run = 0
        for won, _, _ in reversed(self._win):
            if won: break
            run += 1
        return {"n": n, "wins": raw_w, "win_rate": (raw_w / n) if n else 0.0,
                "breakeven": be, "threshold": thr, "prob": p, "loss_run": run}

    # ── state machine ───────────────────────────────────────────────────
    def _evaluate(self, forced_reason: Optional[str]) -> None:
        nums = self._numbers()
        now = self._clock()
        held = now - self._since
        if self._open:
            if nums["loss_run"] >= self.loss_run:
                self._set(False, f"last {nums['loss_run']} results all losses", nums)
            elif nums["prob"] < self.close_prob and held >= self.min_open:
                self._set(False, f"P={nums['prob']:.2f} < {self.close_prob:.2f}", nums)
        else:
            if (nums["n"] >= self.min_trades and nums["prob"] >= self.open_prob
                    and nums["loss_run"] < self.loss_run
                    and held >= self.min_closed):
                self._set(True, f"P={nums['prob']:.2f} >= {self.open_prob:.2f}", nums)

    def _set(self, new_open: bool, reason: str, nums: dict) -> None:
        if new_open == self._open:
            return
        self._open = new_open
        self._since = self._clock()
        self._reason = reason
        if new_open:
            self.cycles += 1
        self.history.append((self._since, "OPEN" if new_open else "CLOSED", reason))
        del self.history[:-50]
        logger.warning(
            f"EDGE GATE {'OPEN ✅' if new_open else 'CLOSED ⛔'} — {reason} | "
            f"n={nums['n']} wins={nums['wins']} wr={nums['win_rate']*100:.1f}% "
            f"breakeven={nums['breakeven']*100:.2f}% "
            f"need>{nums['threshold']*100:.2f}% P={nums['prob']:.3f}")

    # ── public reads ────────────────────────────────────────────────────
    def is_open(self) -> bool:
        if not self._open:
            return False
        if (self.stale_secs > 0 and self._last_result_ts
                and self._clock() - self._last_result_ts > self.stale_secs):
            self._set(False, f"stale: no Scout result for {self.stale_secs:.0f}s",
                      self._numbers())
            return False
        return self._open

    def state(self) -> dict:
        nums = self._numbers()
        nums.update({"open": self._open, "since": self._since,
                     "held_secs": self._clock() - self._since,
                     "reason": self._reason, "cycles": self.cycles,
                     "ignored_unconfirmed": self.ignored_unconfirmed})
        return nums

    # ── persistence ─────────────────────────────────────────────────────
    def export(self) -> dict:
        return {"win": [[int(w), round(r, 4), round(t, 1)] for w, r, t in self._win],
                "seen": list(self._seen), "open": self._open, "since": self._since,
                "reason": self._reason, "last": self._last_result_ts,
                "cycles": self.cycles,
                "hist": [[round(t, 1), st, r] for t, st, r in self.history[-20:]]}

    def load(self, d: dict) -> None:
        try:
            self._win.clear()
            for w, r, t in d.get("win", []):
                self._win.append((bool(w), float(r), float(t)))
            self._seen.extend(str(x) for x in d.get("seen", []))
            self._open = bool(d.get("open", False))
            self._since = float(d.get("since", self._clock()))
            self._reason = str(d.get("reason", "restored"))
            self._last_result_ts = float(d.get("last", 0.0))
            self.cycles = int(d.get("cycles", 0))
            self.history = [(float(t), str(st), str(r)) for t, st, r in d.get("hist", [])]
            logger.info(f"EDGE GATE: restored {len(self._win)} results, "
                        f"{'OPEN' if self._open else 'CLOSED'}")
        except Exception as exc:
            logger.warning(f"EDGE GATE: could not restore state: {exc}")

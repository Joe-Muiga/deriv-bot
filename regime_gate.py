"""
regime_gate.py — fast, rule-based HOT / COLD detector for the Scout's results.
NO ML in the decision itself: a handful of explainable rules over the last few
confirmed Scout settlements. (ml side lives in online_learner.py / auto_tuner.py
and only (a) vetoes weak entries and (b) nudges the numbers below.)

WHY THIS REPLACES THE BETA EDGE GATE
  edge_gate.EdgeGate needs ~40 results and P>=0.97 before it opens, then needs
  P to decay below 0.60 before it closes. On a zig-zag Scout that is always
  late in and late out. This gate reacts to the *turn*:

  COLD -> HOT   (all must hold)
      * at least `entry_wins` wins in the last `fast` results   (default 4 of 6)
      * the last result is a win
      * win-rate over the last `slow` results >= `entry_wr_slow` (default 55%)
        -> it is a turn, not a lone lucky blip
      * at least `min_closed_results` results since the last close (anti-whipsaw)
      * warm-up: `warmup` results seen

  HOT -> COLD   (any one)
      * `exit_loss_run` losses in a row                          (default 2)
      * wins in the last `fast` results <= `exit_wins`           (default 3 of 6)
      * equity (sum of R) fell `exit_dd_r` below its peak since the gate
        opened                                                   (default 2.0 R)
      * no Scout result for `stale_secs`  (Scout stopped / redeploy gap)
      * force_close()

  R = pnl / stake  (a win is ~ +1.12, a loss is -1).

Interface is a superset of edge_gate.EdgeGate (on_result, is_open, state,
force_close, export, load, cycles, history) so follower / hub need no rewrite.
"""
import logging
import math
import time
from collections import deque
from typing import Callable, Deque, Optional

import config

logger = logging.getLogger("regime_gate")

DEFAULT_PARAMS = {
    "fast": 6,
    "slow": 12,
    "entry_wins": 4,
    "entry_wr_slow": 0.55,
    "exit_loss_run": 2,
    "exit_wins": 3,
    "exit_dd_r": 2.0,
}

# Search space the auto-tuner may move inside (value lists, in order).
PARAM_GRID = {
    "entry_wins": [3, 4, 5],
    "entry_wr_slow": [0.45, 0.50, 0.55, 0.60, 0.65],
    "exit_loss_run": [1, 2, 3],
    "exit_wins": [2, 3, 4],
    "exit_dd_r": [1.5, 2.0, 3.0, 4.0],
}

HIST = 400   # results kept (persisted) — the tuner replays these


def _cfg(name, default):
    return getattr(config, name, default)


def clean_params(p: dict) -> dict:
    """Merge over defaults, snap tunables to the grid, keep structure legal."""
    out = dict(DEFAULT_PARAMS)
    for k, v in (p or {}).items():
        if k in out:
            out[k] = v
    for k, grid in PARAM_GRID.items():
        out[k] = min(grid, key=lambda g: abs(g - float(out[k])))
        if isinstance(grid[0], int):
            out[k] = int(out[k])
    out["fast"], out["slow"] = int(out["fast"]), int(out["slow"])
    out["entry_wins"] = min(out["entry_wins"], out["fast"])
    out["exit_wins"] = min(out["exit_wins"], out["entry_wins"] - 1)   # hysteresis
    return out


def _clean_rules(p: dict) -> dict:
    return clean_params(p)


class RegimeGate:
    ENGINE = "rules"
    DEFAULTS = DEFAULT_PARAMS
    GRID = PARAM_GRID
    CFG_PREFIX = "REGIME_"

    @classmethod
    def clean(cls, p: dict) -> dict:
        return _clean_rules(p)

    def __init__(self, clock: Callable[[], float] = time.time,
                 params: Optional[dict] = None, quiet: bool = False, **over):
        self._clock = clock
        self.quiet = quiet
        base = {k: _cfg(self.CFG_PREFIX + k.upper(), v) for k, v in self.DEFAULTS.items()}
        base.update(params or {})
        self.params = self.clean(base)
        g = lambda k, d: over.get(k, _cfg("REGIME_" + k.upper(), d))
        self.warmup = int(g("warmup", 12))
        self.min_closed_results = int(g("min_closed_results", 2))
        self.stale_secs = float(over.get("stale_secs", _cfg(self.CFG_PREFIX + "STALE_SECS", _cfg("REGIME_STALE_SECS", 150))))
        self.default_payout = float(over.get("payout", _cfg("EDGE_PAYOUT_MULTIPLE", 2.12)))
        # attributes the hub's startup log / config summary expect
        self.window = self.params.get("slow", 12)
        self.min_trades = self.warmup
        self.open_prob = 0.0
        self.close_prob = 0.0

        self._res: Deque[tuple] = deque(maxlen=HIST)     # (won, r, ts, ratio)
        self._seen: Deque[str] = deque(maxlen=80)
        self._open = False
        self._since = self._clock()
        self._reason = "initial"
        self._last_result_ts = 0.0
        self._since_change = 10 ** 6       # results since the last open/close flip
        self.eq = 0.0                      # cumulative R of all results seen
        self._peak = 0.0                   # eq peak since the gate opened
        self.cycles = 0
        self.ignored_unconfirmed = 0
        self.history: list = []

    # ── evidence ────────────────────────────────────────────────────────
    def on_result(self, ev: dict) -> None:
        if not ev.get("confirmed", True):
            self.ignored_unconfirmed += 1
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
            return
        stake = float(ev.get("stake", 0) or 0)
        payout = float(ev.get("payout", 0) or 0)
        ratio = payout / stake if stake > 0 and payout > stake else 0.0
        r = (pnl / stake) if stake > 0 else (1.0 if pnl > 0 else -1.0)
        r = max(-1.0, min(3.0, r))
        now = float(ev.get("ts", 0) or 0) or self._clock()
        self._res.append((pnl > 0, r, now, ratio))
        self.eq += r
        self._last_result_ts = self._clock()
        self._since_change += 1
        if self._open:
            self._peak = max(self._peak, self.eq)
        self._learn(pnl > 0, now)
        self._evaluate()

    def _learn(self, won: bool, now: float) -> None:
        """Hook for engines that keep their own belief state."""

    def force_close(self, reason: str) -> None:
        if self._open:
            self._set(False, f"forced: {reason}")

    # ── numbers ─────────────────────────────────────────────────────────
    def _tail(self, n: int) -> list:
        k = len(self._res)
        return [self._res[i] for i in range(max(0, k - n), k)]

    def breakeven(self) -> float:
        inv = [1.0 / x[3] for x in self._res if x[3] > 1.0]
        return (sum(inv) / len(inv)) if inv else 1.0 / self.default_payout

    def loss_run(self) -> int:
        run = 0
        for x in reversed(self._res):
            if x[0]:
                break
            run += 1
        return run

    def numbers(self) -> dict:
        p = self.params
        fast = self._tail(p["fast"])
        slow = self._tail(p["slow"])
        wf = sum(1 for x in fast if x[0])
        ws = sum(1 for x in slow if x[0])
        return {"n": len(self._res), "wins_fast": wf, "n_fast": len(fast),
                "wr_fast": (wf / len(fast)) if fast else 0.0,
                "wr_slow": (ws / len(slow)) if slow else 0.0, "n_slow": len(slow),
                "loss_run": self.loss_run(),
                "last_won": bool(self._res[-1][0]) if self._res else False,
                "dd_r": max(0.0, self._peak - self.eq) if self._open else 0.0}

    def features(self) -> dict:
        """Snapshot used by the ML learner at Scout-entry time."""
        def wr(n):
            t = self._tail(n)
            return (sum(1 for x in t if x[0]) / len(t)) if t else 0.5
        run_w = 0
        for x in reversed(self._res):
            if not x[0]:
                break
            run_w += 1
        t6 = self._tail(6)
        t25 = self._tail(25)
        peak = 0.0; cur = 0.0; mx = 0.0
        for x in t25:
            cur += x[1]; mx = max(mx, cur)
        return {"wr3": wr(3), "wr6": wr(6), "wr12": wr(12), "wr25": wr(25),
                "loss_run": self.loss_run(), "win_run": run_w,
                "slope6": (sum(x[1] for x in t6) / len(t6)) if t6 else 0.0,
                "dd25": max(0.0, mx - cur), "open": 1.0 if self._open else 0.0}

    # ── state machine ───────────────────────────────────────────────────
    def _evaluate(self) -> None:
        p = self.params
        nm = self.numbers()
        if self._open:
            why = None
            if nm["loss_run"] >= p["exit_loss_run"]:
                why = f"{nm['loss_run']} losses in a row"
            elif nm["n_fast"] >= p["fast"] and nm["wins_fast"] <= p["exit_wins"]:
                why = f"cooling: {nm['wins_fast']}/{nm['n_fast']} wins"
            elif nm["dd_r"] >= p["exit_dd_r"]:
                why = f"equity -{nm['dd_r']:.1f}R from peak"
            if why:
                self._set(False, why)
        else:
            if (nm["n"] >= self.warmup and nm["n_fast"] >= p["fast"]
                    and nm["wins_fast"] >= p["entry_wins"] and nm["last_won"]
                    and nm["wr_slow"] >= p["entry_wr_slow"]
                    and self._since_change >= self.min_closed_results):
                self._set(True, f"hot: {nm['wins_fast']}/{nm['n_fast']} fast, "
                                f"{nm['wr_slow']*100:.0f}% slow")

    def _set(self, new_open: bool, reason: str) -> None:
        if new_open == self._open:
            return
        self._open = new_open
        self._since = self._clock()
        self._reason = reason
        self._since_change = 0
        if new_open:
            self.cycles += 1
            self._peak = self.eq
        if not self.quiet:
            self.history.append((self._since, "OPEN" if new_open else "CLOSED", reason))
            del self.history[:-50]
            logger.warning(f"REGIME {'HOT 🔥 OPEN' if new_open else 'COLD ⛔ CLOSED'} — {reason} "
                           f"| eq={self.eq:+.2f}R n={len(self._res)}")

    # ── reads ───────────────────────────────────────────────────────────
    def is_open(self) -> bool:
        if not self._open:
            return False
        if (self.stale_secs > 0 and self._last_result_ts
                and self._clock() - self._last_result_ts > self.stale_secs):
            self._set(False, f"stale: no Scout result for {self.stale_secs:.0f}s")
            return False
        return True

    def state(self) -> dict:
        nm = self.numbers()
        be = self.breakeven()
        return {"n": nm["n_slow"], "wins": sum(1 for x in self._tail(self.params["slow"]) if x[0]),
                "win_rate": nm["wr_slow"], "breakeven": be, "threshold": self.params.get("entry_wr_slow", 0.0),
                "prob": nm["wins_fast"] / nm["n_fast"] if nm["n_fast"] else 0.0,   # "heat" 0..1
                "loss_run": nm["loss_run"], "open": self._open, "since": self._since,
                "held_secs": self._clock() - self._since, "reason": self._reason,
                "cycles": self.cycles, "ignored_unconfirmed": self.ignored_unconfirmed,
                "phase": "HOT" if self._open else "COLD", "wins_fast": nm["wins_fast"],
                "n_fast": nm["n_fast"], "dd_r": round(nm["dd_r"], 2),
                "eq_r": round(self.eq, 2), "params": dict(self.params)}

    def set_params(self, params: dict) -> dict:
        self.params = self.clean({**self.params, **(params or {})})
        self.window = self.params.get("slow", 12)
        return self.params

    def results_compact(self) -> list:
        """[[won, r, ts, ratio], ...] oldest -> newest, for the tuner."""
        return [[int(w), round(r, 3), int(t), round(ra, 3)] for w, r, t, ra in self._res]

    # ── persistence ─────────────────────────────────────────────────────
    def export(self) -> dict:
        return {"res": self.results_compact(), "seen": list(self._seen)[-40:],
                "open": self._open, "since": round(self._since, 1), "reason": self._reason,
                "last": round(self._last_result_ts, 1), "cycles": self.cycles,
                "eq": round(self.eq, 3), "peak": round(self._peak, 3),
                "sc": min(self._since_change, 10 ** 6), "params": self.params,
                "hist": [[round(t, 1), st, r] for t, st, r in self.history[-20:]]}

    def load(self, d: dict) -> None:
        try:
            self._res.clear()
            for w, r, t, ra in d.get("res", []):
                self._res.append((bool(w), float(r), float(t), float(ra)))
            self._seen.extend(str(x) for x in d.get("seen", []))
            self._open = bool(d.get("open", False))
            self._since = float(d.get("since", self._clock()))
            self._reason = str(d.get("reason", "restored"))
            self._last_result_ts = float(d.get("last", 0.0))
            self.cycles = int(d.get("cycles", 0))
            self.eq = float(d.get("eq", 0.0))
            self._peak = float(d.get("peak", self.eq))
            self._since_change = int(d.get("sc", 10 ** 6))
            self.set_params(d.get("params") or {})
            self.history = [(float(t), str(st), str(r)) for t, st, r in d.get("hist", [])]
            # a redeploy gap can swallow a regime change: if the evidence is
            # old by the time we are back, do not trust an OPEN gate.
            if self._open and self._last_result_ts and \
                    self._clock() - self._last_result_ts > float(_cfg("REGIME_RELOAD_MAX_GAP_SECS", 300)):
                self._set(False, "restored after a long gap — re-confirm first")
            logger.info(f"REGIME: restored {len(self._res)} results, "
                        f"{'HOT' if self._open else 'COLD'}, params={self.params}")
        except Exception as exc:
            logger.warning(f"REGIME: could not restore state: {exc}")


# ═════════════════════════════════════════════════════════════════════════
#  HMM engine (Oct 2026) — time-aware Bayesian hot/cold tracker
# ═════════════════════════════════════════════════════════════════════════
#  Why: the rule gate counts RESULTS (needs 6-12 of them to turn), so on a
#  zig-zag whose hot/cold sessions last seconds to a few minutes it arrives
#  after the session is over. This engine keeps ONE number instead:
#       b = P(Scout is in a HOT session right now)
#  updated by Bayes after every confirmed result and DECAYED TOWARD 50/50 by
#  the real time that has passed (a session may have flipped while nothing
#  settled). From b it forecasts the next trade's win probability
#       q = b*p_hot + (1-b)*p_cold
#  and trades only while q beats the payout breakeven by a margin:
#       open  when q - breakeven >= enter_margin
#       close when q - breakeven <  exit_margin       (exit < enter = hysteresis)
#
#  LEARNING (the "ML"): every `refit_every` results the gate re-estimates, by
#  maximum likelihood (Baum-Welch / EM on the recent window, time-aware), the
#  Scout's real p_hot, p_cold and typical session length. It then runs a
#  likelihood-ratio test against "no regimes at all" (one constant win rate).
#  If the zig-zag is not statistically real, p_hot = p_cold = the average win
#  rate, the gap is 0 and the gate stays CLOSED. "No structure here" is an
#  answer, not a failure. The auto-tuner then tunes only the margins.
MS_GRID = [10.0, 20.0, 40.0, 80.0, 160.0, 320.0]      # session lengths (secs) the fit chooses from

HMM_DEFAULTS = {
    "fast": 6, "slow": 12,                 # only for the shared display numbers
    "enter_margin": 0.03,
    "exit_margin": 0.0,
    "mean_session": 40.0,                  # FITTED from data (see refit), grid-snapped
    "exit_dd_r": 3.0,
}
HMM_GRID = {
    "enter_margin": [0.0, 0.02, 0.03, 0.05, 0.08],
    "exit_margin": [-0.06, -0.03, 0.0, 0.02],
    "mean_session": MS_GRID,
    "exit_dd_r": [2.0, 3.0, 4.0],
}
HMM_TUNABLE = ("enter_margin", "exit_margin", "exit_dd_r")


def hmm_fit(ys: list, ts: list, ph: float, pc: float, ms: float, iters: int = 8,
            prior=(0.62, 0.36), n0: float = 2.0):
    """Time-aware 2-state HMM, symmetric switching with mean session `ms` seconds.
    Returns (ph, pc, loglik, P(hot) after the last result). Scaled forward-backward EM."""
    n = len(ys)
    if n < 2:
        return ph, pc, 0.0, 0.5
    stay = [0.5] + [0.5 + 0.5 * math.exp(-2.0 * max(0.0, ts[i] - ts[i - 1]) / ms)
                    for i in range(1, n)]

    def forward(ph, pc):
        al = [0.0] * n; sc = [1.0] * n
        h = 0.5; ll = 0.0
        for i in range(n):
            if i:
                h = h * stay[i] + (1 - h) * (1 - stay[i])
            lh = ph if ys[i] else 1 - ph
            lc = pc if ys[i] else 1 - pc
            a, b = h * lh, (1 - h) * lc
            z = a + b or 1e-300
            h = a / z; al[i] = h; sc[i] = z; ll += math.log(z)
        return al, sc, ll

    for _ in range(iters):
        al, sc, _ll = forward(ph, pc)
        # backward on the hot-probability scale (beta_hot, beta_cold normalised per step)
        bh = bc = 1.0
        gam = [0.0] * n
        for i in range(n - 1, -1, -1):
            a = al[i]
            # gamma_i ∝ alpha_i * beta_i  (alpha is already the normalised hot posterior)
            g = a * bh; g2 = (1 - a) * bc
            gam[i] = g / ((g + g2) or 1e-300)
            if i:
                lh = ph if ys[i] else 1 - ph
                lc = pc if ys[i] else 1 - pc
                s = stay[i]
                nbh = s * lh * bh + (1 - s) * lc * bc          # from hot at i-1
                nbc = (1 - s) * lh * bh + s * lc * bc          # from cold at i-1
                z = (nbh + nbc) or 1e-300
                bh, bc = nbh / z, nbc / z
        wh = sum(gam); wc = n - wh
        yh = sum(g for g, y in zip(gam, ys) if y)
        yc = sum(1 for y in ys if y) - yh
        ph = min(0.98, max(0.02, (yh + n0 * prior[0]) / (wh + n0)))
        pc = min(0.98, max(0.02, (yc + n0 * prior[1]) / (wc + n0)))
        if ph < pc:
            ph, pc = pc, ph
    al, sc, ll = forward(ph, pc)
    return ph, pc, ll, al[-1]


class HMMGate(RegimeGate):
    ENGINE = "hmm"
    DEFAULTS = HMM_DEFAULTS
    GRID = HMM_GRID
    CFG_PREFIX = "HMM_"

    @classmethod
    def clean(cls, p: dict) -> dict:
        out = dict(HMM_DEFAULTS)
        for k, v in (p or {}).items():
            if k in out:
                out[k] = v
        for k, grid in HMM_GRID.items():
            out[k] = min(grid, key=lambda g: abs(g - float(out[k])))
        out["fast"], out["slow"] = int(out["fast"]), int(out["slow"])
        ok = [g for g in HMM_GRID["exit_margin"] if g <= out["enter_margin"] - 0.02]
        out["exit_margin"] = max(ok) if ok else HMM_GRID["exit_margin"][0]   # hysteresis
        return out

    def __init__(self, clock: Callable[[], float] = time.time,
                 params: Optional[dict] = None, quiet: bool = False, **over):
        super().__init__(clock=clock, params=params, quiet=quiet, **over)
        g = lambda k, d: over.get(k, _cfg("HMM_" + k.upper(), d))
        self.warmup = int(g("warmup", 40))                 # results before the first fit
        self.min_closed_results = int(g("min_closed_results", 1))
        self.prior_hot = float(g("prior_hot", 0.62))
        self.prior_cold = float(g("prior_cold", 0.36))
        self.refit_every = int(g("refit_every", 20))
        self.fit_window = int(g("fit_window", 300))
        self.fit_iters = int(g("fit_iters", 12))
        self.min_sep = float(g("min_sep", 0.12))
        self.lr_min = float(g("lr_min", 5.0))             # 2*dLogLik needed vs "no regimes"
        self.belief = 0.5
        self._bt = 0.0                                     # timestamp the belief refers to
        self.ph, self.pc = self.prior_hot, self.prior_cold
        self.fitted = False                                # a significant fit exists
        self.fit_lr = 0.0
        self.fits = 0
        self._since_fit = 0

    # ── belief maths ────────────────────────────────────────────────────
    def _decay(self, b: float, dt: float) -> float:
        k = 2.0 / max(1.0, float(self.params["mean_session"]))
        return 0.5 + (b - 0.5) * math.exp(-k * max(0.0, dt))

    def belief_now(self, at: Optional[float] = None) -> float:
        at = self._clock() if at is None else at
        return self._decay(self.belief, (at - self._bt) if self._bt else 0.0)

    def separation(self) -> float:
        return (self.ph - self.pc) if self.fitted else 0.0

    def q_now(self, at: Optional[float] = None) -> float:
        b = self.belief_now(at)
        return b * self.ph + (1.0 - b) * self.pc

    def edge_now(self, at: Optional[float] = None) -> float:
        return self.q_now(at) - self.breakeven()

    def _learn(self, won: bool, now: float) -> None:
        b = self._decay(self.belief, (now - self._bt) if self._bt else 0.0)
        lh = self.ph if won else 1.0 - self.ph
        lc = self.pc if won else 1.0 - self.pc
        den = b * lh + (1.0 - b) * lc
        self.belief = (b * lh / den) if den > 0 else 0.5
        self._bt = now
        self._since_fit += 1
        if len(self._res) >= self.warmup and self._since_fit >= self.refit_every:
            self.refit()

    def refit(self) -> dict:
        """Maximum-likelihood refit of p_hot / p_cold / session length on the recent window."""
        self._since_fit = 0
        rows = list(self._res)[-self.fit_window:]
        ys = [1 if r[0] else 0 for r in rows]
        ts = [float(r[2]) for r in rows]
        n = len(ys)
        if n < 30:
            return {}
        p_all = (sum(ys) + 1.0) / (n + 2.0)
        l0 = sum(math.log(p_all if y else 1 - p_all) for y in ys)
        best = None
        starts = [(self.prior_hot, self.prior_cold), (0.72, 0.28)]
        if self.fitted:
            starts.insert(0, (self.ph, self.pc))             # warm start from the last fit
        prior = (self.prior_hot, self.prior_cold)
        # coarse: every (session length, start) with few EM steps; fine: refine only the best two
        coarse = []
        for ms in MS_GRID:
            for s_ph, s_pc in starts:
                ph, pc, ll, b = hmm_fit(ys, ts, s_ph, s_pc, ms, iters=3, prior=prior)
                coarse.append((ll, ms, ph, pc))
        coarse.sort(reverse=True)
        for _ll, ms, s_ph, s_pc in coarse[:2]:
            ph, pc, ll, b = hmm_fit(ys, ts, s_ph, s_pc, ms, iters=self.fit_iters, prior=prior)
            if best is None or ll > best[2]:
                best = (ph, pc, ll, b, ms)
        ph, pc, ll, b, ms = best
        lr = 2.0 * (ll - l0)
        self.fits += 1
        self.fit_lr = lr
        was = self.fitted
        if lr >= self.lr_min and (ph - pc) >= self.min_sep:
            self.ph, self.pc, self.fitted = ph, pc, True
            self.params["mean_session"] = ms
            self.belief, self._bt = b, ts[-1]
        else:
            self.ph = self.pc = p_all
            self.fitted = False
        if not self.quiet and (was != self.fitted or self.fits % 10 == 1):
            logger.warning(f"HMM FIT #{self.fits}: p_hot={ph:.2f} p_cold={pc:.2f} session~{ms:.0f}s "
                           f"LR={lr:.1f} (need {self.lr_min:.0f}) -> "
                           f"{'REAL regimes' if self.fitted else 'no significant regimes'}")
        return {"ph": ph, "pc": pc, "ms": ms, "lr": lr, "fitted": self.fitted}

    # ── state machine ───────────────────────────────────────────────────
    def _evaluate(self) -> None:
        p = self.params
        nm = self.numbers()
        edge = self.edge_now(self._bt or None)
        if self._open:
            why = None
            if not self.fitted:
                why = f"no significant regime structure (LR {self.fit_lr:.1f})"
            elif edge < p["exit_margin"]:
                why = f"edge {edge*100:+.1f}pt, P(hot)={self.belief:.2f}"
            elif nm["dd_r"] >= p["exit_dd_r"]:
                why = f"equity -{nm['dd_r']:.1f}R from peak"
            if why:
                self._set(False, why)
        elif (self.fitted and nm["n"] >= self.warmup and edge >= p["enter_margin"]
              and self._since_change >= self.min_closed_results):
            self._set(True, f"hot: P(hot)={self.belief:.2f} q={self.q_now(self._bt or None)*100:.0f}% "
                            f"edge {edge*100:+.1f}pt")

    def is_open(self) -> bool:
        if not self._open:
            return False
        if (self.stale_secs > 0 and self._last_result_ts
                and self._clock() - self._last_result_ts > self.stale_secs):
            self._set(False, f"stale: no Scout result for {self.stale_secs:.0f}s")
            return False
        e = self.edge_now()
        if e < self.params["exit_margin"]:
            self._set(False, f"evidence aged out: edge {e*100:+.1f}pt")
            return False
        return True

    def edge_series(self, rows: list) -> list:
        """[(edge, r), ...] — the edge the live filter would have shown after each stored
        result ([[won, r, ts, ratio], ...]). Thread-safe: touches no live state. The tuner
        scores margin candidates on it without refitting."""
        tmp = HMMGate(clock=time.time, params=self.params, quiet=True,
                      warmup=self.warmup, refit_every=self.refit_every,
                      fit_window=self.fit_window, fit_iters=self.fit_iters,
                      min_sep=self.min_sep, lr_min=self.lr_min, stale_secs=0)
        out = []
        for w, r, ts, ra in rows:
            tmp.on_result({"pnl": r, "stake": 1.0, "payout": ra, "ts": ts, "confirmed": True})
            out.append((tmp.edge_now(ts) if tmp.fitted else -1.0, r))
        return out

    # ── reporting / persistence ─────────────────────────────────────────
    def features(self) -> dict:
        f = super().features()
        f.update({"belief": self.belief_now(), "edge": self.edge_now() if self.fitted else -0.1,
                  "sep": self.separation(),
                  "gap": (self._clock() - self._bt) if self._bt else 600.0})
        return f

    def state(self) -> dict:
        s = super().state()
        s.update({"engine": "hmm", "belief": round(self.belief_now(), 3),
                  "prob": round(self.belief_now(), 3), "q": round(self.q_now(), 3),
                  "edge": round(self.edge_now(), 3), "p_hot": round(self.ph, 3),
                  "p_cold": round(self.pc, 3), "sep": round(self.separation(), 3),
                  "regimes_real": self.fitted, "fit_lr": round(self.fit_lr, 1),
                  "session_secs": self.params["mean_session"], "fits": self.fits,
                  "threshold": self.breakeven() + self.params["enter_margin"]})
        return s

    def export(self) -> dict:
        d = super().export()
        d["hmm"] = {"b": round(self.belief, 4), "bt": round(self._bt, 1), "ph": round(self.ph, 4),
                    "pc": round(self.pc, 4), "fit": int(self.fitted), "lr": round(self.fit_lr, 2),
                    "fits": self.fits}
        return d

    def load(self, d: dict) -> None:
        super().load(d)
        try:
            h = d.get("hmm") or {}
            if h:
                self.belief = float(h.get("b", 0.5)); self._bt = float(h.get("bt", 0.0))
                self.ph = float(h.get("ph", self.ph)); self.pc = float(h.get("pc", self.pc))
                self.fitted = bool(h.get("fit", 0)); self.fit_lr = float(h.get("lr", 0.0))
                self.fits = int(h.get("fits", 0))
            elif len(self._res) >= self.warmup:        # saved by the rules engine: rebuild
                self.refit()
            if self._open and not self.fitted:
                self._set(False, "restored without a significant fit")
        except Exception as exc:
            logger.warning(f"HMM: could not restore belief: {exc}")


def build_gate(engine: Optional[str] = None, **kw) -> RegimeGate:
    engine = str(engine or _cfg("REGIME_ENGINE", "hmm")).lower()
    return (HMMGate if engine == "hmm" else RegimeGate)(**kw)

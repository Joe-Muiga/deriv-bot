"""
auto_tuner.py — learns the regime gate's numbers from the bot's own history.

Every `ML_TUNE_EVERY` new confirmed Scout results it REPLAYS the last ~400
results through candidate gate settings and scores each by what a follower
would have made (sum of R on the trades it would have taken, minus a small
penalty per open/close flip). It then moves ONE step at a time (coordinate
search on the grid in regime_gate.PARAM_GRID) and only ADOPTS a change if:

  * it beats the current settings on the older 60% of the history, AND
  * it is not worse on the newest 40% (walk-forward check), AND
  * the total gain is at least `ML_TUNE_MIN_GAIN_R`, AND
  * it would have taken at least `ML_TUNE_MIN_TAKEN` trades.

It also produces an honest verdict, `no_edge`: if, on the newest 40%, the
gated follower would have LOST (and enough trades were taken), the follower is
told to stand aside (when config.REGIME_EDGE_BRAKE is on). The tuner cannot
create an edge that is not in the data — it only finds the best timing of one
that is.
"""
import logging
import threading
import time
from typing import List, Optional

import config
from regime_gate import RegimeGate, HMMGate, PARAM_GRID, HMM_TUNABLE, clean_params

logger = logging.getLogger("auto_tuner")


def _cfg(name, default):
    return getattr(config, name, default)


def replay(results: List[list], params: dict, lag: int = 1, min_closed_results: int = 2,
           warmup: int = 12, flip_penalty: float = 0.1, split: Optional[int] = None) -> dict:
    """results: [[won, r, ts, ratio], ...] oldest->newest. Returns R scores.
    The decision for result i uses the gate state after result i-1-lag
    (entries are placed before the most recent settlements are known)."""
    t = [0.0]
    g = RegimeGate(clock=lambda: t[0], params=params, quiet=True, warmup=warmup,
                   min_closed_results=min_closed_results, stale_secs=0)
    open_after: List[bool] = []
    tot = {"R": 0.0, "taken": 0, "wins": 0}
    seg = {"R": 0.0, "taken": 0, "wins": 0}      # newest segment (index >= split)
    base_R = 0.0; flips = 0
    prev = False
    for i, (w, r, ts, ra) in enumerate(results):
        j = i - 1 - lag
        gate_open = open_after[j] if j >= 0 else False
        base_R += r
        if gate_open:
            tot["R"] += r; tot["taken"] += 1; tot["wins"] += int(bool(w))
            if split is not None and i >= split:
                seg["R"] += r; seg["taken"] += 1; seg["wins"] += int(bool(w))
        t[0] = float(ts)
        g.on_result({"pnl": r, "stake": 1.0, "payout": ra, "contract_id": "",
                     "confirmed": True})
        open_after.append(g._open)
        if g._open != prev:
            flips += 1; prev = g._open
    tot["score"] = tot["R"] - flip_penalty * flips
    tot["flips"] = flips; tot["always_R"] = base_R
    tot["seg"] = seg
    return tot


def replay_hmm(edges: List[tuple], params: dict, lag: int = 1, flip_penalty: float = 0.1,
               split: Optional[int] = None) -> dict:
    """Score margin settings on a cached [(edge, r), ...] series: same open/close state
    machine as HMMGate._evaluate, entries decided on the state `lag` settles earlier."""
    en, ex, dd_lim = params["enter_margin"], params["exit_margin"], params["exit_dd_r"]
    tot = {"R": 0.0, "taken": 0, "wins": 0}
    seg = {"R": 0.0, "taken": 0, "wins": 0}
    open_after: List[bool] = []
    is_open = False; eq = 0.0; peak = 0.0; flips = 0; base_R = 0.0
    for i, (edge, r) in enumerate(edges):
        j = i - 1 - lag
        if j >= 0 and open_after[j]:
            tot["R"] += r; tot["taken"] += 1; tot["wins"] += int(r > 0)
            if split is not None and i >= split:
                seg["R"] += r; seg["taken"] += 1; seg["wins"] += int(r > 0)
        base_R += r
        eq += r
        if is_open:
            peak = max(peak, eq)
            if edge < ex or (peak - eq) >= dd_lim:
                is_open = False; flips += 1
        elif edge >= en:
            is_open = True; flips += 1; peak = eq
        open_after.append(is_open)
    tot["score"] = tot["R"] - flip_penalty * flips
    tot["flips"] = flips; tot["always_R"] = base_R; tot["seg"] = seg
    return tot


class AutoTuner:
    def __init__(self, gate: RegimeGate, clock=time.time):
        self.gate = gate
        self._clock = clock
        self._since_tune = 0
        self._busy = False
        self.runs = 0
        self.adopted = 0
        self.no_edge = False
        self.last: dict = {}
        self.log: list = []          # [[ts, "param old->new", gainR], ...] last 20

    def note_result(self) -> None:
        self._since_tune += 1

    def due(self) -> bool:
        return (not self._busy and
                self._since_tune >= int(_cfg("ML_TUNE_EVERY", 60)) and
                len(self.gate._res) >= int(_cfg("ML_TUNE_MIN_RESULTS", 120)))

    def maybe_tune(self, background: bool = True) -> bool:
        if not bool(_cfg("ML_TUNER_ENABLED", True)) or not self.due():
            return False
        self._busy = True
        self._since_tune = 0
        data = self.gate.results_compact()
        cur = dict(self.gate.params)
        if background:
            threading.Thread(target=self._run, args=(data, cur), daemon=True,
                             name="auto-tuner").start()
        else:
            self._run(data, cur)
        return True

    # ── the search ──────────────────────────────────────────────────────
    def _run(self, data: list, cur: dict) -> None:
        try:
            lag = int(_cfg("REGIME_SETTLE_LAG", 1))
            kw = dict(lag=lag, min_closed_results=self.gate.min_closed_results,
                      warmup=self.gate.warmup,
                      flip_penalty=float(_cfg("ML_TUNE_FLIP_PENALTY", 0.1)))
            n = len(data)
            split = int(n * 0.6)
            min_taken = int(_cfg("ML_TUNE_MIN_TAKEN", 20))
            min_gain = float(_cfg("ML_TUNE_MIN_GAIN_R", 1.0))

            hmm = getattr(self.gate, "ENGINE", "rules") == "hmm"
            if hmm:
                edges = self.gate.edge_series(data)       # fitted once; margins are cheap to score
                grid_all = {k: self.gate.GRID[k] for k in HMM_TUNABLE}
                clean = self.gate.clean

                def score(p):
                    return replay_hmm(edges, p, lag=lag, split=split,
                                      flip_penalty=kw["flip_penalty"])
            else:
                grid_all = PARAM_GRID
                clean = clean_params

                def score(p):
                    return replay(data, p, split=split, **kw)

            base = score(cur)
            best_p, best = dict(cur), base
            for _ in range(2):                                   # two coordinate passes
                improved = False
                for k, grid in grid_all.items():
                    i0 = grid.index(best_p[k]) if best_p[k] in grid else 0
                    for ii in (i0 - 1, i0 + 1):
                        if not 0 <= ii < len(grid):
                            continue
                        cand = clean({**best_p, k: grid[ii]})
                        if cand == best_p:
                            continue
                        s = score(cand)
                        if s["score"] > best["score"] + 1e-9:
                            best_p, best, improved = cand, s, True
                if not improved:
                    break
            self.runs += 1
            seg_b, seg_c = base["seg"], best["seg"]
            adopt = (best_p != cur
                     and best["score"] - base["score"] >= min_gain
                     and seg_c["R"] >= seg_b["R"]
                     and best["taken"] >= min_taken)
            if adopt:
                self.gate.set_params(best_p)
                self.adopted += 1
                diff = ", ".join(f"{k} {cur[k]}->{best_p[k]}" for k in best_p if best_p[k] != cur.get(k))
                self.log.append([int(self._clock()), diff, round(best["score"] - base["score"], 2)])
                del self.log[:-20]
                logger.warning(f"AUTO-TUNER adopted: {diff} | replay score "
                               f"{base['score']:+.1f}R -> {best['score']:+.1f}R "
                               f"(newest 40%: {seg_b['R']:+.1f} -> {seg_c['R']:+.1f}R)")
                final = best
            else:
                final = base
            seg = final["seg"]
            self.no_edge = bool(seg["taken"] >= int(_cfg("ML_NO_EDGE_MIN_TAKEN", 20))
                                and seg["R"] < 0)
            self.last = {"ts": int(self._clock()), "n": n, "gated_R": round(final["R"], 2),
                         "always_R": round(final["always_R"], 2), "taken": final["taken"],
                         "flips": final["flips"], "newest_R": round(seg["R"], 2),
                         "newest_taken": seg["taken"], "adopted": bool(adopt),
                         "no_edge": self.no_edge, "params": dict(self.gate.params)}
            logger.warning(f"AUTO-TUNER verdict: gated {final['R']:+.1f}R on {final['taken']} trades "
                           f"vs always-follow {final['always_R']:+.1f}R | newest 40%: "
                           f"{seg['R']:+.1f}R on {seg['taken']} | no_edge={self.no_edge}")
        except Exception as exc:
            logger.warning(f"AUTO-TUNER failed: {exc}")
        finally:
            self._busy = False

    # ── persistence ─────────────────────────────────────────────────────
    def export(self) -> dict:
        return {"runs": self.runs, "adopted": self.adopted, "no_edge": self.no_edge,
                "last": self.last, "log": self.log[-20:], "since": self._since_tune}

    def load(self, d: dict) -> None:
        self.runs = int(d.get("runs", 0)); self.adopted = int(d.get("adopted", 0))
        self.no_edge = bool(d.get("no_edge", False)); self.last = d.get("last") or {}
        self.log = list(d.get("log") or []); self._since_tune = int(d.get("since", 0))

    def state(self) -> dict:
        return {"runs": self.runs, "adopted": self.adopted, "no_edge": self.no_edge,
                "last": self.last, "log": self.log[::-1][:8], "params": dict(self.gate.params),
                "next_in": max(0, int(_cfg("ML_TUNE_EVERY", 60)) - self._since_tune)}

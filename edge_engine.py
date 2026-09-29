"""
edge_engine.py -- out-of-sample statistical edge gate for Donkey digit trades.

Purpose: a trade is only allowed when the recorded tick history for the
symbol shows a *demonstrated* advantage for the exact contract about to be
bought, at the payout actually on offer. It never picks or changes the
contract (trading logic untouched); it only says GO / NO-GO.

Method (walk-forward, dependency-free):
  1. Turn the last N digits into win/loss outcomes for the candidate
     contract (OVER b wins on d > b, UNDER b wins on d < b).
  2. Split chronologically: first TRAIN_FRAC = fit window, rest = untouched
     confirmation window.
  3. Two hypotheses are tested (Bonferroni-corrected for the 2 looks):
       marginal : P(win) over all ticks
       markov1  : P(win | previous digit == current last digit)
  4. A hypothesis passes only if the Wilson score LOWER confidence bound of
     the win rate beats the break-even rate 1/(1+payout_ratio) in BOTH the
     fit window and the confirmation window, each with >= min samples.
  5. The reported p_win_lb is the smaller of the two lower bounds -- the
     conservative win probability used for the buy-time EV check.

On a fair random digit stream this gate almost never opens (that is the
correct answer: there is nothing to exploit). It opens only when a symbol's
digits are measurably biased in a way that survives out-of-sample.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import List, Optional


@dataclass
class EdgeVerdict:
    go: bool
    reason: str
    p_win_lb: float = 0.0     # conservative (lower-bound) win probability
    model: str = ""
    n_train: int = 0
    n_test: int = 0


def wilson_lower_bound(k: int, n: int, z: float) -> float:
    if n <= 0:
        return 0.0
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def breakeven_prob(payout_ratio: float) -> float:
    """Win prob at which a contract paying `payout_ratio` x stake profit
    (plus stake back) has zero expected value."""
    return 1.0 / (1.0 + payout_ratio)


def evaluate_edge(
    digits: List[int],
    match_type: str,
    barrier: int,
    payout_ratio: float,
    *,
    alpha: float = 1e-5,
    min_ticks: int = 1500,
    train_frac: float = 0.6,
    min_ctx_samples: int = 60,
    ev_margin: float = 0.0,
) -> EdgeVerdict:
    n = len(digits)
    if n < min_ticks:
        return EdgeVerdict(False, f"history {n} < {min_ticks} ticks")
    if match_type == "OVER":
        w = [1 if d > barrier else 0 for d in digits]
    elif match_type == "UNDER":
        w = [1 if d < barrier else 0 for d in digits]
    else:
        return EdgeVerdict(False, f"unsupported contract {match_type}")

    p_be = breakeven_prob(payout_ratio) + ev_margin
    z = NormalDist().inv_cdf(1.0 - (alpha / 2.0))   # one-sided, /2 models
    split = int(n * train_frac)
    cur = digits[-1]

    hypotheses = {
        "marginal": range(1, n),
        "markov1": [i for i in range(1, n) if digits[i - 1] == cur],
    }
    best: Optional[EdgeVerdict] = None
    notes = []
    for name, idxs in hypotheses.items():
        tr = [w[i] for i in idxs if i < split]
        te = [w[i] for i in idxs if i >= split]
        if len(tr) < min_ctx_samples or len(te) < min_ctx_samples:
            notes.append(f"{name}: too few samples ({len(tr)}/{len(te)})")
            continue
        lb_tr = wilson_lower_bound(sum(tr), len(tr), z)
        lb_te = wilson_lower_bound(sum(te), len(te), z)
        if lb_tr > p_be and lb_te > p_be:
            v = EdgeVerdict(True, f"{name} edge confirmed", min(lb_tr, lb_te),
                            name, len(tr), len(te))
            if best is None or v.p_win_lb > best.p_win_lb:
                best = v
        else:
            notes.append(f"{name}: lb fit={lb_tr:.3f} test={lb_te:.3f} <= be={p_be:.3f}")
    if best:
        return best
    return EdgeVerdict(False, "no out-of-sample edge (" + "; ".join(notes) + ")")


def expected_value_per_stake(p_win: float, payout_ratio: float) -> float:
    return p_win * payout_ratio - (1.0 - p_win)

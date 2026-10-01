"""
balance_tiers.py — balance-banded stake ladder, profit-target pause table and
Donkey-guard loss thresholds, in ONE table so the three always agree.

Rows 1-13 are the user's own numbers (Sep 2026 chat). Rows 14+ are
extrapolated up to a $50,000 account with a $2,000 maximum stake:

  * Stake ladder      : 12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 125, 150, 200,
                        250, 300, 400, 500, 600, 800, 1000, 1250, 1500, 2000.
  * Band ceiling      : stake / ratio, where ratio = stake as a fraction of the
                        band's top balance. The user's last row is $10 at $160
                        (6.25%). The ratio slides log-linearly from 6.25% down
                        to 4.00% so that $2,000 lands exactly on $50,000.
  * Profit target     : multiple-of-stake. The user's top rows run ~6-7x stake
                        ($70 at $10 = 7x). The multiple slides 7x -> 5x by
                        $2,000 (big accounts bank smaller % per session).
  * Guard stop-loss   : up to $10 stake: min(12 stakes, 40% of the band's floor
                        balance / stake), never below 5. Above $10: flat 5
                        stakes. Old default was a flat 15 stakes, which at
                        these stake sizes is bigger than the account.
  * Guard loss streak : 7 in a row (stake <= $1), 6 (<= $10), 5 above. Old: 8.

Every extrapolated number is a plain formula below — change a constant and the
whole table regenerates. Nothing else in the bot hard-codes these values.

Public API
  tier_for_balance(balance) -> Tier
  stake_for_balance(balance) -> float
  tier_for_stake(stake)      -> Tier   (highest tier whose stake <= given)
  profit_target_for_balance(balance) -> float
  guard_params_for_stake(stake) -> dict
  TIERS                      -> list[Tier]
"""

import math
from dataclasses import dataclass
from typing import List

# ── User-supplied rows: (balance_low, balance_high, stake, profit_target) ────
# NOTE: the message listed "$3 stake -> $15" which duplicated the $2 row; $18
# (6x stake, between the $2 and $4 rows) is used. Edit here if you meant 15.
_USER_ROWS = [
    (0.00,   8.00,   0.35,  3.0),
    (8.01,  15.00,   0.50,  4.5),
    (15.01, 25.00,   0.70,  6.0),
    (25.01, 36.00,   1.00, 10.0),
    (36.01, 50.00,   2.00, 15.0),
    (50.01, 64.00,   3.00, 18.0),
    (64.01, 80.00,   4.00, 22.0),
    (80.01, 94.00,   5.00, 30.0),
    (94.01, 106.00,  6.00, 36.0),
    (106.01, 120.00, 7.00, 44.0),
    (120.01, 132.00, 8.00, 56.0),
    (132.01, 142.00, 9.00, 60.0),
    (142.01, 160.00, 10.00, 70.0),
]

# ── Extrapolation constants ──────────────────────────────────────────────────
MAX_ACCOUNT   = 50_000.0
MAX_STAKE     = 2_000.0
_EXTRA_STAKES = [12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 125, 150, 200, 250,
                 300, 400, 500, 600, 800, 1000, 1250, 1500, 2000]
_RATIO_START  = 10.0 / 160.0     # stake / band-top at the user's last row
_RATIO_END    = MAX_STAKE / MAX_ACCOUNT   # 4.00%
_MULT_START   = 7.0              # profit target in stakes at $10
_MULT_END     = 5.0              # profit target in stakes at $2,000

GUARD_SL_MAX_STAKES        = 12      # was 15
GUARD_SL_MAX_PCT_OF_FLOOR  = 0.40    # stop-loss <= 40% of band-floor balance
GUARD_SL_MIN_STAKES        = 5
PAUSE_MIN_MINUTES          = 11      # pause length drawn uniformly at random
PAUSE_MAX_MINUTES          = 18      # between these two (was a fixed 45)


@dataclass(frozen=True)
class Tier:
    n: int
    bal_low: float
    bal_high: float
    stake: float
    profit_target: float      # $ session profit that triggers the pause
    sl_stakes: int            # donkey guard session stop-loss, in stakes
    loss_streak: int          # donkey guard consecutive-loss limit
    extrapolated: bool

    @property
    def target_in_stakes(self) -> float:
        return self.profit_target / self.stake

    @property
    def sl_dollars(self) -> float:
        return self.sl_stakes * self.stake


def _t(s: float) -> float:
    """0 at stake $10, 1 at stake $2,000 (log scale)."""
    return math.log(s / 10.0) / math.log(MAX_STAKE / 10.0)


def _nice_bound(x: float) -> float:
    step = 10 if x < 1000 else 100 if x < 10_000 else 500
    return float(round(x / step) * step)


def _nice_target(x: float) -> float:
    step = 5 if x < 1000 else 50 if x < 10_000 else 500
    return float(round(x / step) * step)


def _loss_streak(stake: float) -> int:
    return 7 if stake <= 1 else 6 if stake <= 10 else 5


def _sl_stakes(stake: float, bal_low: float, bal_high: float) -> int:
    if stake > 10:                      # extrapolated region: flat 5 stakes
        return GUARD_SL_MIN_STAKES      # (<= ~28% of the band floor)
    floor_bal = bal_low if bal_low > 0 else bal_high
    n = int(GUARD_SL_MAX_PCT_OF_FLOOR * floor_bal / stake)
    return max(GUARD_SL_MIN_STAKES, min(GUARD_SL_MAX_STAKES, n))


def _build() -> List[Tier]:
    rows = [(lo, hi, s, tgt, False) for lo, hi, s, tgt in _USER_ROWS]
    prev_hi = _USER_ROWS[-1][1]
    for s in _EXTRA_STAKES:
        t = _t(s)
        ratio = _RATIO_START + (_RATIO_END - _RATIO_START) * t
        hi = MAX_ACCOUNT if s == MAX_STAKE else _nice_bound(s / ratio)
        mult = _MULT_START + (_MULT_END - _MULT_START) * t
        rows.append((round(prev_hi + 0.01, 2), hi, float(s),
                     _nice_target(mult * s), True))
        prev_hi = hi
    return [Tier(i + 1, lo, hi, s, tgt, _sl_stakes(s, lo, hi),
                 _loss_streak(s), ex)
            for i, (lo, hi, s, tgt, ex) in enumerate(rows)]


TIERS: List[Tier] = _build()


# ── Lookups ──────────────────────────────────────────────────────────────────

def tier_for_balance(balance: float) -> Tier:
    if balance is None or not math.isfinite(balance) or balance <= 0:
        return TIERS[0]
    for t in TIERS:
        if balance <= t.bal_high:
            return t
    return TIERS[-1]            # above $50k: stay on the $2,000 cap


def stake_for_balance(balance: float) -> float:
    return tier_for_balance(balance).stake


def profit_target_for_balance(balance: float) -> float:
    return tier_for_balance(balance).profit_target


def tier_for_stake(stake: float) -> Tier:
    best = TIERS[0]
    for t in TIERS:
        if t.stake <= stake + 1e-9:
            best = t
    return best


def guard_params_for_stake(stake: float) -> dict:
    t = tier_for_stake(stake)
    return {"sl_stakes": t.sl_stakes, "loss_streak": t.loss_streak}


if __name__ == "__main__":
    print(f"{'#':>2} {'balance band':>21} {'stake':>7} {'pause @ profit':>15} "
          f"{'x stake':>7} {'SL (stakes)':>11} {'SL $':>9} {'streak':>6}")
    for t in TIERS:
        band = f"{t.bal_low:,.2f}-{t.bal_high:,.2f}"
        print(f"{t.n:>2} {band:>21} {t.stake:>7g} {t.profit_target:>15,.1f} "
              f"{t.target_in_stakes:>7.1f} {t.sl_stakes:>11d} "
              f"{t.sl_dollars:>9,.2f} {t.loss_streak:>6d}"
              + ("  *" if t.extrapolated else ""))

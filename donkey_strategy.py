"""
donkey_strategy.py
===================

Directional-inversion overlay derived from 1000 logged live trades
(217 wins / 783 losses) showing the underlying directional read was
wrong far more often than it was right, while stop-loss/exit handling
was sound. Applied as the LAST step after normal analysis, so every
upstream evaluator, meta-labeling gate, and stats/dashboard consumer
keeps operating on the same pipeline as before — only direction (and,
for Multiplier contracts, the SL/TP shape) changes at the very end.

Everything upstream of the call site in signal_engine.SignalEngine.evaluate()
is untouched: strategy routing, indicator reads, underperformance gating,
and logging all run exactly as before. This module only flips the final
LONG/SHORT direction and, for Multiplier-family contracts, substitutes a
tighter stop / wider target sizing at buy time.

Not applied to:
  - contract_kind == "DIGIT" (Matches/Differs has no LONG/SHORT read)
  - direction not in ("LONG", "SHORT")
  - any strategy name in DONKEY_EXCLUDED_STRATEGIES
"""

from __future__ import annotations

import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)

_FLIP = {"LONG": "SHORT", "SHORT": "LONG"}


def enabled() -> bool:
    return bool(getattr(config, "DONKEY_STRATEGY_ENABLED", False))


def _eligible(sig) -> bool:
    if not enabled():
        return False
    if getattr(sig, "contract_kind", "RISE_FALL") != "RISE_FALL":
        return False
    if getattr(sig, "direction", "NONE") not in _FLIP:
        return False
    excluded = getattr(config, "DONKEY_EXCLUDED_STRATEGIES", [])
    if getattr(sig, "strategy", "") in excluded:
        return False
    allowed = getattr(config, "DONKEY_STRATEGIES", None)
    if allowed is not None and getattr(sig, "strategy", "") not in allowed:
        return False
    return True


def maybe_invert(sig):
    """
    Called once, at the single choke point (end of SignalEngine.evaluate()),
    on every signal that already passed threshold/underperformance gating.
    Returns sig unchanged if not eligible; otherwise returns a new
    SignalResult with direction flipped. `strategy` and `reason` text are
    left as-is so every downstream consumer (dashboard, journal, stats)
    reads exactly like a normal signal from the named strategy.
    """
    if not _eligible(sig):
        return sig

    new_direction = _FLIP[sig.direction]
    logger.info(
        f"DONKEY INVERT: {sig.strategy} {sig.direction} -> {new_direction} "
        f"(score={sig.score:.3f})"
    )
    return type(sig)(
        direction=new_direction,
        strength=sig.strength,
        score=sig.score,
        strategy=sig.strategy,
        reason=sig.reason,
        contract_kind=sig.contract_kind,
        digit=sig.digit,
        match_type=sig.match_type,
    )


def is_donkey_symbol_strategy(strategy: str) -> bool:
    """Used at buy-time (Multiplier branch) to decide whether to apply the
    tight-stop/wide-target sizing below. Mirrors _eligible()'s strategy
    filter without needing the SignalResult object."""
    if not enabled():
        return False
    excluded = getattr(config, "DONKEY_EXCLUDED_STRATEGIES", [])
    if strategy in excluded:
        return False
    allowed = getattr(config, "DONKEY_STRATEGIES", None)
    if allowed is not None and strategy not in allowed:
        return False
    return True


def stop_loss_pct(strategy: str, base_pct: float) -> float:
    """
    Tighten the stop relative to whatever the base pipeline would have
    used (STOP_LOSS_MAP / DEFAULT_STOP_LOSS_PCT, or the dynamic ATR-based
    pct for VOL_MULTIPLIER_SYMBOLS). Floored at DONKEY_MIN_SL_PCT so it
    never collapses to an unfillable stop.
    """
    if not is_donkey_symbol_strategy(strategy):
        return base_pct
    fraction = getattr(config, "DONKEY_SL_TIGHTEN_FRACTION", 0.35)
    floor = getattr(config, "DONKEY_MIN_SL_PCT", 8.0)
    return max(floor, base_pct * fraction)


def take_profit_ratio(strategy: str, base_ratio: float) -> float:
    """
    Widen the profit target relative to TAKE_PROFIT_RATIO for donkey
    trades — small, tight loss vs. a large aimed-for win, matching the
    >0.75 stop-hit / ~0.2 target-hit split observed in the source stats.
    """
    if not is_donkey_symbol_strategy(strategy):
        return base_ratio
    return getattr(config, "DONKEY_TAKE_PROFIT_RATIO", 6.0)

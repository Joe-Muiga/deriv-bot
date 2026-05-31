"""
signal_engine.py — Strategy routing + signal evaluation engine.

Changes (v5 → v6):
  - Logging added at every decision point
  - Fallback strategy (EMA20/50 crossover) for unrecognised symbols
  - Digit  : min score lowered  6 → 4
  - Mean Rev: conditions OR not AND (2-of-3)
  - Range Break: consolidation optional  (strength 3 vs 2)
  - Boom/Crash: spike threshold  3.0× → 1.5× ATR
  - Step Index: EMA crossover alone is enough (no Donchian gate)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

import numpy as np

import config
import indicators as ind

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    """Unified signal envelope returned by every evaluate_* function."""

    direction: str          # "LONG" | "SHORT" | "NONE"
    strength: int           # 0–3
    sl: float               # stop-loss distance (price units)
    tp: float               # take-profit distance (price units)
    score: float            # normalised confidence  0.0–1.0
    risk_reward: float      # tp / sl  (0 when sl == 0)
    reason: str             # human-readable label

    @staticmethod
    def none(reason: str = "no signal") -> "SignalResult":
        return SignalResult("NONE", 0, 0.0, 0.0, 0.0, 0.0, reason)


# ---------------------------------------------------------------------------
# Digit Over/Under
# ---------------------------------------------------------------------------

def evaluate_digit(ltf_bars, symbol: str) -> SignalResult:
    """
    Score last 8 closes against the mid-point.
    Fires when score >= 4  (was 6).
    """
    logger.info(f"EVALUATING: {symbol}")
    closes = [b.close for b in ltf_bars[-8:]]
    if len(closes) < 8:
        logger.info(f"DIGIT REJECTED: {symbol} insufficient bars")
        return SignalResult.none("insufficient bars")

    mid = (max(closes) + min(closes)) / 2.0
    over_count  = sum(1 for c in closes if c > mid)
    under_count = sum(1 for c in closes if c < mid)

    if over_count > under_count:
        direction = "LONG"
        raw_score = over_count
    elif under_count > over_count:
        direction = "SHORT"
        raw_score = under_count
    else:
        raw_score = 0
        direction = "NONE"

    # ---- CHANGED: threshold lowered from 6 → 4 ----
    if raw_score < 4:
        logger.info(
            f"DIGIT REJECTED: {symbol} score={raw_score}/8 below 4"
        )
        return SignalResult(
            "NONE", 0, 0.0, 0.0, raw_score / 8.0, 0.0,
            f"digit score {raw_score}/8 < 4"
        )

    atr = _atr14(ltf_bars)
    sl  = atr * 1.5
    tp  = atr * 2.0
    rr  = tp / sl if sl else 0.0

    logger.info(f"DIGIT EMITTED: {symbol} {direction} score={raw_score}/8")
    return SignalResult(
        direction, 2, sl, tp, raw_score / 8.0, rr,
        f"digit {direction} score={raw_score}/8"
    )


# ---------------------------------------------------------------------------
# Mean Reversion
# ---------------------------------------------------------------------------

def evaluate_mean_reversion(ltf_bars, symbol: str) -> SignalResult:
    """
    RSI / Bollinger / ROC mean-reversion.
    Fires when ANY 2 of 3 conditions are met  (was: all 3).
    """
    logger.info(f"EVALUATING: {symbol}")
    closes = np.array([b.close for b in ltf_bars])
    if len(closes) < 50:
        return SignalResult.none("insufficient bars")

    rsi_val = ind.rsi(closes, 14)[-1]
    upper_bb, lower_bb = ind.bollinger_bands(closes, 20, 2.0)
    roc_val = ind.roc(closes, 10)[-1]
    price   = closes[-1]

    # Individual conditions (direction-agnostic here; resolved below)
    rsi_long   = rsi_val < 30
    rsi_short  = rsi_val > 70
    bb_long    = price < lower_bb[-1]
    bb_short   = price > upper_bb[-1]
    roc_long   = roc_val < -2.0
    roc_short  = roc_val > 2.0

    long_conditions  = [rsi_long,  bb_long,  roc_long]
    short_conditions = [rsi_short, bb_short, roc_short]

    long_met  = sum(long_conditions)
    short_met = sum(short_conditions)

    # ---- CHANGED: ANY 2-of-3 is enough (was: all 3) ----
    if long_met >= 2:
        direction     = "LONG"
        conditions_met = long_met
    elif short_met >= 2:
        direction     = "SHORT"
        conditions_met = short_met
    else:
        conditions_met = max(long_met, short_met)
        logger.info(
            f"MR REJECTED: {symbol} conditions={conditions_met}/3"
        )
        return SignalResult.none(f"MR conditions={conditions_met}/3")

    atr = _atr14(ltf_bars)
    sl  = atr * 1.5
    tp  = atr * 3.0
    rr  = tp / sl if sl else 0.0

    logger.info(
        f"MR EMITTED: {symbol} {direction} conditions={conditions_met}/3"
    )
    return SignalResult(
        direction, 2, sl, tp, conditions_met / 3.0, rr,
        f"mean-reversion {direction} {conditions_met}/3"
    )


# ---------------------------------------------------------------------------
# Range Break
# ---------------------------------------------------------------------------

def evaluate_range_break(ltf_bars, symbol: str) -> SignalResult:
    """
    Breakout + RSI confirmation.
    consolidation now only required for strength=3  (was: always required).
    """
    logger.info(f"EVALUATING: {symbol}")
    closes = np.array([b.close for b in ltf_bars])
    highs  = np.array([b.high  for b in ltf_bars])
    lows   = np.array([b.low   for b in ltf_bars])

    if len(closes) < 30:
        return SignalResult.none("insufficient bars")

    rsi_val = ind.rsi(closes, 14)[-1]
    recent_high = highs[-21:-1].max()
    recent_low  = lows[-21:-1].min()
    price       = closes[-1]

    if price > recent_high:
        direction        = "LONG"
        breakout_confirmed = True
        rsi_confirmed    = rsi_val > 55
    elif price < recent_low:
        direction        = "SHORT"
        breakout_confirmed = True
        rsi_confirmed    = rsi_val < 45
    else:
        breakout_confirmed = False
        rsi_confirmed    = False
        direction        = "NONE"

    # Consolidation: narrow range over last 10 bars
    range_10     = highs[-11:-1].max() - lows[-11:-1].min()
    range_20     = highs[-21:-1].max() - lows[-21:-1].min()
    consolidation_confirmed = range_10 < range_20 * 0.5

    # ---- CHANGED: breakout + RSI alone → strength 2; add consolidation → strength 3 ----
    if breakout_confirmed and rsi_confirmed:
        strength = 3 if consolidation_confirmed else 2
        atr = _atr14(ltf_bars)
        sl  = atr * 1.5
        tp  = atr * (3.0 if strength == 3 else 2.0)
        rr  = tp / sl if sl else 0.0
        logger.info(
            f"RB EMITTED: {symbol} {direction} strength={strength}"
        )
        return SignalResult(
            direction, strength, sl, tp, 0.8, rr,
            f"range-break {direction} strength={strength}"
        )

    logger.info(
        f"RB REJECTED: {symbol} "
        f"breakout={breakout_confirmed} rsi={rsi_confirmed}"
    )
    return SignalResult.none(
        f"range-break rejected breakout={breakout_confirmed} rsi={rsi_confirmed}"
    )


# ---------------------------------------------------------------------------
# Boom / Crash
# ---------------------------------------------------------------------------

SPIKE_ATR_MULTIPLIER = 1.5   # CHANGED: was 3.0


def evaluate_boom_crash(ltf_bars, symbol: str) -> SignalResult:
    """
    Spike detection on Boom/Crash indices.
    Threshold reduced from 3.0× → 1.5× ATR.
    """
    logger.info(f"EVALUATING: {symbol}")
    if len(ltf_bars) < 15:
        return SignalResult.none("insufficient bars")

    atr14     = _atr14(ltf_bars)
    last_bar  = ltf_bars[-1]
    bar_move  = abs(last_bar.high - last_bar.low)
    threshold = atr14 * SPIKE_ATR_MULTIPLIER

    logger.info(
        f"SPIKE CHECK: {symbol} "
        f"bar_move={bar_move:.4f} threshold={threshold:.4f}"
    )

    if bar_move <= threshold:
        return SignalResult.none(
            f"boom/crash spike below threshold "
            f"bar_move={bar_move:.4f} < {threshold:.4f}"
        )

    # Determine direction from spike body
    body = last_bar.close - last_bar.open
    direction = "LONG" if body > 0 else "SHORT"

    sl = atr14 * 1.0
    tp = atr14 * 2.0
    rr = tp / sl if sl else 0.0

    logger.info(
        f"BOOM_CRASH EMITTED: {symbol} {direction} "
        f"bar_move={bar_move:.4f} threshold={threshold:.4f}"
    )
    return SignalResult(
        direction, 3, sl, tp, min(bar_move / threshold, 1.0), rr,
        f"boom/crash spike {direction}"
    )


# ---------------------------------------------------------------------------
# Step Index
# ---------------------------------------------------------------------------

def evaluate_step(ltf_bars, symbol: str) -> SignalResult:
    """
    EMA crossover signal for Step Index.
    EMA crossover alone → strength 2  (Donchian confirmation no longer required).
    """
    logger.info(f"EVALUATING: {symbol}")
    closes = np.array([b.close for b in ltf_bars])
    if len(closes) < 55:
        return SignalResult.none("insufficient bars")

    ema_fast_arr = ind.ema(closes, 10)
    ema_slow_arr = ind.ema(closes, 30)

    if len(ema_fast_arr) < 2 or len(ema_slow_arr) < 2:
        return SignalResult.none("insufficient EMA data")

    ema_fast = ema_fast_arr[-1]
    ema_slow = ema_slow_arr[-1]

    atr = _atr14(ltf_bars)
    sl  = atr * 1.5
    tp  = atr * 2.5
    rr  = tp / sl if sl else 0.0

    # ---- CHANGED: EMA crossover alone is sufficient ----
    if ema_fast > ema_slow:
        direction = "LONG"
        logger.info(
            f"STEP EMITTED: {symbol} LONG "
            f"ema_fast={ema_fast:.4f} > ema_slow={ema_slow:.4f}"
        )
    elif ema_fast < ema_slow:
        direction = "SHORT"
        logger.info(
            f"STEP EMITTED: {symbol} SHORT "
            f"ema_fast={ema_fast:.4f} < ema_slow={ema_slow:.4f}"
        )
    else:
        return SignalResult.none("step ema_fast == ema_slow")

    return SignalResult(
        direction, 2, sl, tp, 0.7, rr,
        f"step EMA crossover {direction}"
    )


# ---------------------------------------------------------------------------
# Fallback — EMA20/50 crossover (any unrecognised symbol)
# ---------------------------------------------------------------------------

def evaluate_fallback(ltf_bars, symbol: str) -> SignalResult:
    """
    Simple EMA20 vs EMA50 crossover.
    Fires on any symbol not handled by a specific strategy.
    """
    logger.info(f"EVALUATING: {symbol}")
    closes = np.array([b.close for b in ltf_bars])
    ema20 = ind.ema(closes, 20)
    ema50 = ind.ema(closes, 50)

    if len(ema20) < 2 or len(ema50) < 2:
        logger.info(f"FALLBACK REJECTED: {symbol} insufficient data")
        return SignalResult("NONE", 0, 0.0, 0.0, 0.0, 0.0, "insufficient data")

    crossed_long  = ema20[-1] > ema50[-1] and ema20[-2] <= ema50[-2]
    crossed_short = ema20[-1] < ema50[-1] and ema20[-2] >= ema50[-2]

    if crossed_long:
        direction = "LONG"
    elif crossed_short:
        direction = "SHORT"
    else:
        logger.info(f"FALLBACK REJECTED: {symbol} no crossover")
        return SignalResult("NONE", 0, 0.0, 0.0, 0.0, 0.0, "no crossover")

    logger.info(f"FALLBACK EMITTED: {symbol} {direction}")
    return SignalResult(
        direction, 2, 0.0, 0.0, 1.0, 0.0,
        f"EMA crossover {direction}"
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SignalEngine:

    def evaluate(self, ltf_bars: List, symbol: str, **kwargs) -> SignalResult:
        """
        Route to the correct strategy and return a SignalResult.

        Strategy map:
          DIGIT_SYMBOLS       → evaluate_digit
          BOOM_CRASH_SYMBOLS  → evaluate_boom_crash
          RANGE_BREAK_SYMBOLS → evaluate_range_break
          STEP_SYMBOLS        → evaluate_step
          JUMP_SYMBOLS        → evaluate_fallback   (CHANGED: was None)
          DRIFT_SYMBOLS       → evaluate_fallback   (CHANGED: was None)
          <unrecognised>      → evaluate_fallback   (CHANGED: was NONE silent)
        """
        logger.info(
            f"SIGNAL EVAL START: {symbol} bars={len(ltf_bars)}"
        )

        if symbol in config.DIGIT_SYMBOLS:
            strategy_name = "digit"
            result = evaluate_digit(ltf_bars, symbol)

        elif symbol in config.BOOM_CRASH_SYMBOLS:
            strategy_name = "boom_crash"
            result = evaluate_boom_crash(ltf_bars, symbol)

        elif symbol in config.RANGE_BREAK_SYMBOLS:
            strategy_name = "range_break"
            result = evaluate_range_break(ltf_bars, symbol)

        elif symbol in config.STEP_SYMBOLS:
            strategy_name = "step"
            result = evaluate_step(ltf_bars, symbol)

        elif symbol in config.JUMP_SYMBOLS:
            strategy_name = "fallback(jump)"
            result = evaluate_fallback(ltf_bars, symbol)

        elif symbol in config.DRIFT_SYMBOLS:
            strategy_name = "fallback(drift)"
            result = evaluate_fallback(ltf_bars, symbol)

        else:
            # ---- CHANGED: no longer silently returns NONE ----
            strategy_name = "fallback(unrecognised)"
            result = evaluate_fallback(ltf_bars, symbol)

        logger.info(f"STRATEGY: {symbol} → {strategy_name}")
        logger.info(
            f"SIGNAL EVAL END: {symbol} → "
            f"{result.direction} strength={result.strength} "
            f"score={result.score:.3f}"
        )
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _atr14(ltf_bars) -> float:
    """True-range ATR over the last 14 bars."""
    bars = ltf_bars[-15:]
    trs  = []
    for i in range(1, len(bars)):
        high  = bars[i].high
        low   = bars[i].low
        prev_close = bars[i - 1].close
        trs.append(max(high - low,
                       abs(high - prev_close),
                       abs(low  - prev_close)))
    return float(np.mean(trs)) if trs else 0.0

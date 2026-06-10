"""
signal_engine.py
Momentum-based signal generation for Deriv
synthetic indices using Multiplier contracts.

Strategy per symbol type:
  Volatility (R_10–R_100, 1HZ): EMA momentum
    + breakout detection
  Boom/Crash: drift direction after spike
  Step Index: EMA trend following
  Jump Index: pre-jump momentum window
  Range Break: breakout confirmation
  Drift Switch: regime direction
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
    direction:   str    # "LONG"|"SHORT"|"NONE"
    strength:    int    # 1-3
    score:       float  # 0.0-1.0
    strategy:    str
    reason:      str
    stop_loss:   float  # dollar amount for SL
    take_profit: float  # dollar amount for TP
    multiplier:  int    # recommended multiplier


# ---------------------------------------------------------------------------
# NONE_RESULT constant
# ---------------------------------------------------------------------------

NONE_RESULT = SignalResult(
    "NONE", 0, 0.0, "NONE", "No signal",
    0.0, 0.0, 0)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SignalEngine:

    def __init__(self, *args, **kwargs):
        pass

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def evaluate(self, ltf_bars: List, mtf_bars: List,
                 symbol: str, stake: float = 1.0,
                 **kwargs) -> SignalResult:
        """
        Route to the correct strategy by symbol type and return a SignalResult.

        Routing:
          VOLATILITY_STANDARD + VOLATILITY_1S +
          STEP + JUMP + DRIFT                  → _evaluate_volatility
          BOOM_CRASH                           → _evaluate_boom_crash
          RANGE_BREAK                          → _evaluate_range_break
          <unrecognised>                       → _evaluate_volatility (fallback)
        """
        logger.info(
            f"SIGNAL EVAL START: {symbol} "
            f"ltf_bars={len(ltf_bars)} stake={stake}"
        )

        if len(ltf_bars) < 15:
            logger.info(
                f"SIGNAL EVAL REJECTED: {symbol} "
                f"insufficient bars ({len(ltf_bars)} < 15)"
            )
            return NONE_RESULT

        if symbol in (config.VOLATILITY_STANDARD +
                      config.VOLATILITY_1S +
                      config.STEP + config.JUMP +
                      config.DRIFT):
            result = self._evaluate_volatility(
                ltf_bars, mtf_bars, symbol, stake)

        elif symbol in config.BOOM_CRASH:
            result = self._evaluate_boom_crash(
                ltf_bars, symbol, stake)

        elif symbol in config.RANGE_BREAK:
            result = self._evaluate_range_break(
                ltf_bars, symbol, stake)

        else:
            logger.info(
                f"SIGNAL EVAL: {symbol} unrecognised — "
                f"falling back to volatility strategy"
            )
            result = self._evaluate_volatility(
                ltf_bars, mtf_bars, symbol, stake)

        logger.info(
            f"SIGNAL EVAL END: {symbol} → "
            f"{result.direction} strength={result.strength} "
            f"score={result.score:.3f} mult={result.multiplier}x"
        )
        return result

    # -----------------------------------------------------------------------
    # Volatility indices — EMA momentum + breakout
    # -----------------------------------------------------------------------

    def _evaluate_volatility(self, ltf_bars: List, mtf_bars: List,
                              symbol: str, stake: float) -> SignalResult:
        """
        EMA momentum + breakout detection for Volatility / Step / Jump / Drift.

        1. momentum_score() → (score, direction) from LTF closes/highs/lows
        2. detect_breakout() → breakout confirmation from ATR on LTF
        3. detect_trend_strength() → ranging-market penalty
        4. Combine: breakout agreement → +0.2; weak trend → ×0.7
        5. Score filter: reject below config.MIN_SIGNAL_SCORE
        6. SL/TP from STOP_LOSS_MAP and TAKE_PROFIT_RATIO; multiplier from
           MULTIPLIER_MAP.
        """
        logger.info(f"EVALUATING VOLATILITY: {symbol}")

        C = np.array([b.close for b in ltf_bars])
        H = np.array([b.high  for b in ltf_bars])
        L = np.array([b.low   for b in ltf_bars])

        # --- Momentum score from LTF ---
        score, direction = ind.momentum_score(
            C, H, L,
            fast=config.EMA_FAST,
            slow=config.EMA_SLOW,
            trend=config.EMA_TREND)

        if direction == 0:
            logger.info(
                f"VOLATILITY REJECTED: {symbol} "
                f"momentum direction=0"
            )
            return NONE_RESULT

        # --- Breakout confirmation from LTF ATR ---
        atr_arr = ind.atr(H, L, C, config.ATR_PERIOD)
        valid_atr = atr_arr[~np.isnan(atr_arr)]
        atr = float(valid_atr[-1]) if len(valid_atr) else 0.0

        breakout = ind.detect_breakout(
            C, H, L, atr_arr,
            lookback=config.MOMENTUM_LOOKBACK,
            mult=config.BREAKOUT_ATR_MULT)

        # --- Trend strength filter ---
        strength_val = ind.detect_trend_strength(C)

        # --- Combine signals ---
        if breakout != 0 and breakout == direction:
            score = min(score + 0.2, 1.0)

        if strength_val < 0.3:
            score *= 0.7  # penalise ranging market

        if score < config.MIN_SIGNAL_SCORE:
            logger.info(
                f"VOLATILITY REJECTED: {symbol} "
                f"score={score:.3f} < "
                f"{config.MIN_SIGNAL_SCORE}"
            )
            return NONE_RESULT

        # --- SL / TP / multiplier ---
        sl_pct = config.STOP_LOSS_MAP.get(
            symbol, config.DEFAULT_STOP_LOSS_PCT)
        sl_amt = round(stake * sl_pct / 100, 2)
        tp_amt = round(sl_amt * config.TAKE_PROFIT_RATIO, 2)
        mult   = config.MULTIPLIER_MAP.get(
            symbol, config.DEFAULT_MULTIPLIER)

        dir_str = "LONG" if direction > 0 else "SHORT"

        logger.info(
            f"VOLATILITY SIGNAL: {symbol} {dir_str} "
            f"score={score:.3f} "
            f"mult={mult}x "
            f"SL=${sl_amt} TP=${tp_amt}"
        )

        return SignalResult(
            direction   = dir_str,
            strength    = 3 if score >= 0.7 else 2,
            score       = score,
            strategy    = "EMA_MOMENTUM",
            reason      = (f"EMA momentum | "
                           f"breakout={breakout} | "
                           f"trend={strength_val:.2f}"),
            stop_loss   = sl_amt,
            take_profit = tp_amt,
            multiplier  = mult,
        )

    # -----------------------------------------------------------------------
    # Boom / Crash — drift direction after spike
    # -----------------------------------------------------------------------

    def _evaluate_boom_crash(self, ltf_bars: List,
                              symbol: str, stake: float) -> SignalResult:
        """
        Detect post-spike drift direction for Boom/Crash indices.

        Uses ind.boom_crash_drift() which analyses the LTF close array
        and returns +1 (bullish drift), -1 (bearish drift), or 0 (none).
        """
        logger.info(f"EVALUATING BOOM/CRASH: {symbol}")

        C = np.array([b.close for b in ltf_bars])
        H = np.array([b.high  for b in ltf_bars])
        L = np.array([b.low   for b in ltf_bars])

        drift = ind.boom_crash_drift(C)
        if drift == 0:
            logger.info(
                f"BOOM/CRASH REJECTED: {symbol} drift=0"
            )
            return NONE_RESULT

        score   = 0.65
        dir_str = "LONG" if drift > 0 else "SHORT"
        sl_pct  = config.STOP_LOSS_MAP.get(
            symbol, config.DEFAULT_STOP_LOSS_PCT)
        sl_amt  = round(stake * sl_pct / 100, 2)
        tp_amt  = round(sl_amt * config.TAKE_PROFIT_RATIO, 2)
        mult    = config.MULTIPLIER_MAP.get(
            symbol, config.DEFAULT_MULTIPLIER)

        logger.info(
            f"BOOM/CRASH SIGNAL: {symbol} {dir_str} "
            f"drift score={score:.3f} "
            f"mult={mult}x "
            f"SL=${sl_amt} TP=${tp_amt}"
        )

        return SignalResult(
            direction   = dir_str,
            strength    = 2,
            score       = score,
            strategy    = "BOOM_CRASH_DRIFT",
            reason      = f"Drift {dir_str} detected",
            stop_loss   = sl_amt,
            take_profit = tp_amt,
            multiplier  = mult,
        )

    # -----------------------------------------------------------------------
    # Range Break — breakout confirmation
    # -----------------------------------------------------------------------

    def _evaluate_range_break(self, ltf_bars: List,
                               symbol: str, stake: float) -> SignalResult:
        """
        ATR-based breakout confirmation for Range Break indices.

        Uses a 15-bar lookback and 1.0× ATR multiplier.
        Returns LONG / SHORT at strength=3 score=0.75 on confirmed breakout.
        """
        logger.info(f"EVALUATING RANGE BREAK: {symbol}")

        C = np.array([b.close for b in ltf_bars])
        H = np.array([b.high  for b in ltf_bars])
        L = np.array([b.low   for b in ltf_bars])

        atr_arr  = ind.atr(H, L, C, config.ATR_PERIOD)
        breakout = ind.detect_breakout(
            C, H, L, atr_arr, lookback=15, mult=1.0)

        if breakout == 0:
            logger.info(
                f"RANGE BREAK REJECTED: {symbol} breakout=0"
            )
            return NONE_RESULT

        dir_str = "LONG" if breakout > 0 else "SHORT"
        sl_amt  = round(
            stake * config.DEFAULT_STOP_LOSS_PCT / 100, 2)
        tp_amt  = round(sl_amt * config.TAKE_PROFIT_RATIO, 2)
        mult    = config.MULTIPLIER_MAP.get(
            symbol, config.DEFAULT_MULTIPLIER)

        logger.info(
            f"RANGE BREAK SIGNAL: {symbol} {dir_str} "
            f"mult={mult}x "
            f"SL=${sl_amt} TP=${tp_amt}"
        )

        return SignalResult(
            direction   = dir_str,
            strength    = 3,
            score       = 0.75,
            strategy    = "RANGE_BREAK",
            reason      = f"Breakout {dir_str}",
            stop_loss   = sl_amt,
            take_profit = tp_amt,
            multiplier  = mult,
        )

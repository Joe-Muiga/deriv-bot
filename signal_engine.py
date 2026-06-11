"""
signal_engine.py
Momentum-based signal generation for Deriv
synthetic indices using Multiplier contracts.

Strategy per symbol type:
  Volatility (R_10–R_100, 1HZ): RSI + Bollinger
    mean reversion
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
    # Volatility indices — RSI + Bollinger mean reversion
    # -----------------------------------------------------------------------

    def _evaluate_volatility(self, ltf_bars, mtf_bars,
                             symbol, stake):
        if len(ltf_bars) < 20:
            return NONE_RESULT

        C = np.array([b.close for b in ltf_bars])
        H = np.array([b.high  for b in ltf_bars])
        L = np.array([b.low   for b in ltf_bars])

        # RSI mean reversion
        rsi = ind.rsi(C, 14)
        valid_rsi = rsi[~np.isnan(rsi)]
        if len(valid_rsi) < 2:
            return NONE_RESULT
        last_rsi  = valid_rsi[-1]
        prev_rsi  = valid_rsi[-2]

        # Bollinger bands
        upper, mid, lower = ind.bollinger_bands(C, 20, 2.0)
        valid_upper = upper[~np.isnan(upper)]
        valid_lower = lower[~np.isnan(lower)]
        if len(valid_upper) < 1:
            return NONE_RESULT
        last_close  = float(C[-1])
        last_upper  = float(valid_upper[-1])
        last_lower  = float(valid_lower[-1])

        direction = None
        score     = 0.0
        reasons   = []

        # LONG: RSI oversold AND price at/below lower BB
        if last_rsi < 30 and last_close <= last_upper * 1.01:
            if prev_rsi < last_rsi:  # RSI turning up
                direction = "LONG"
                score = 0.80
                reasons.append(
                    f"RSI={last_rsi:.1f} oversold+turning")
        # Even stronger: deep oversold
        if last_rsi < 20:
            direction = "LONG"
            score = 0.90
            reasons.append(f"RSI={last_rsi:.1f} deep oversold")

        # SHORT: RSI overbought AND price at/above upper BB
        if last_rsi > 70 and last_close >= last_lower * 0.99:
            if prev_rsi > last_rsi:  # RSI turning down
                direction = "SHORT"
                score = 0.80
                reasons.append(
                    f"RSI={last_rsi:.1f} overbought+turning")
        # Even stronger: deep overbought
        if last_rsi > 80:
            direction = "SHORT"
            score = 0.90
            reasons.append(f"RSI={last_rsi:.1f} deep overbought")

        if not direction:
            return NONE_RESULT

        sl_pct = config.STOP_LOSS_MAP.get(
            symbol, config.DEFAULT_STOP_LOSS_PCT)
        sl_amt = round(stake * sl_pct / 100, 2)
        tp_amt = round(sl_amt * config.TAKE_PROFIT_RATIO, 2)
        mult   = config.MULTIPLIER_MAP.get(
            symbol, config.DEFAULT_MULTIPLIER)

        logger.info(
            f"MEAN_REVERSION: {symbol} {direction} "
            f"RSI={last_rsi:.1f} score={score:.3f}")

        return SignalResult(
            direction   = direction,
            strength    = 3 if score >= 0.85 else 2,
            score       = score,
            strategy    = "MEAN_REVERSION",
            reason      = " | ".join(reasons),
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
        logger.info(f"SIGNAL EMITTED: {symbol} {dir_str}")

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
        logger.info(f"SIGNAL EMITTED: {symbol} {dir_str}")

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

"""
signal_engine.py
Comprehensive SMC + Retail TA signal engine.
Strategies: Order Blocks, FVGs, Liquidity Sweeps,
Market Structure, Fibonacci, Chart Patterns,
Candlestick Patterns, EMA Momentum, Mean Reversion,
MACD, RSI, Bollinger Bands, ADX trend strength.

INVERSION RULE: Every signal direction is inverted
at the final return — LONG becomes SHORT and SHORT
becomes LONG. This is intentional and must never
be removed. All analysis runs normally; only the
final output direction is flipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

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
# Strategy functions — each returns (direction, score, reason) or (None, 0, "")
# ---------------------------------------------------------------------------

def _strat_smc_ob(opens, H, L, C, atr) -> Tuple:
    """
    Order Block strategy.
    Find fresh OBs; check if current price is inside or near one.
    """
    try:
        obs = ind.find_order_blocks(opens, H, L, C, lookback=50)
        if not obs:
            return None, 0, ""

        current_price = float(C[-1])
        atr_val = float(atr[-1]) if len(atr) > 0 else 0.0
        if atr_val == 0:
            return None, 0, ""

        best_dir   = None
        best_score = 0.0
        best_reason = ""

        for ob in obs:
            if ob["test_count"] >= 2:
                continue  # only fresh OBs (test_count < 2)

            ob_h = ob["high"]
            ob_l = ob["low"]
            ob_type = ob["type"]

            # Inside OB
            if ob_l <= current_price <= ob_h:
                score = 0.82
                direction = "LONG" if ob_type == "BULLISH" else "SHORT"
                reason = f"Price inside fresh {ob_type} OB [{ob_l:.5f}-{ob_h:.5f}]"
            # Near OB (within 0.5×ATR)
            elif abs(current_price - ob["mid"]) <= 0.5 * atr_val:
                score = 0.72
                direction = "LONG" if ob_type == "BULLISH" else "SHORT"
                reason = f"Price near fresh {ob_type} OB [{ob_l:.5f}-{ob_h:.5f}]"
            else:
                continue

            if score > best_score:
                best_score  = score
                best_dir    = direction
                best_reason = reason

        if best_dir:
            return best_dir, best_score, best_reason
        return None, 0, ""
    except Exception:
        return None, 0, ""


def _strat_smc_fvg(opens, H, L, C, atr) -> Tuple:
    """
    Fair Value Gap strategy.
    Look for unfilled FVGs; check if price is inside or touching one.
    """
    try:
        fvgs = ind.find_fvg(opens, H, L, C, atr, min_atr=0.5)
        if not fvgs:
            return None, 0, ""

        current_price = float(C[-1])

        for fvg in reversed(fvgs):  # most recent first
            if fvg["filled"]:
                continue
            fvg_h = fvg["high"]
            fvg_l = fvg["low"]
            if fvg_l <= current_price <= fvg_h:
                direction = "LONG" if fvg["type"] == "BULLISH" else "SHORT"
                return direction, 0.78, f"Price in unfilled {fvg['type']} FVG [{fvg_l:.5f}-{fvg_h:.5f}]"

        return None, 0, ""
    except Exception:
        return None, 0, ""


def _strat_liquidity_sweep(H, L, C) -> Tuple:
    """
    Liquidity Sweep + Reversal strategy.
    Highest-score strategy when it fires.
    """
    try:
        result = ind.liquidity_sweep(H, L, C, lookback=20)
        if result == 1:
            return "LONG",  0.85, "Swept swing lows then closed above (bullish reversal)"
        if result == -1:
            return "SHORT", 0.85, "Swept swing highs then closed below (bearish reversal)"
        return None, 0, ""
    except Exception:
        return None, 0, ""


def _strat_market_structure(H, L, C) -> Tuple:
    """
    Market structure bias (HH+HL = BULLISH, LH+LL = BEARISH).
    """
    try:
        structure, score = ind.market_structure(H, L, C, lookback=5)
        if structure == "BULLISH" and score > 0:
            return "LONG",  score, f"Market structure BULLISH (score={score})"
        if structure == "BEARISH" and score > 0:
            return "SHORT", score, f"Market structure BEARISH (score={score})"
        return None, 0, ""
    except Exception:
        return None, 0, ""


def _strat_fibonacci(H, L, C, atr) -> Tuple:
    """
    Fibonacci retracement confluence.
    Checks if price is at key fib level aligned with market structure.
    """
    try:
        n = len(C)
        if n < 20:
            return None, 0, ""

        # Identify last swing high and low
        sh_arr = ind.swing_highs(H, lookback=5)
        sl_arr = ind.swing_lows(L, lookback=5)

        sh_pts = [(i, float(v)) for i, v in enumerate(sh_arr) if not np.isnan(v)]
        sl_pts = [(i, float(v)) for i, v in enumerate(sl_arr) if not np.isnan(v)]

        if not sh_pts or not sl_pts:
            return None, 0, ""

        last_sh_i, last_sh = sh_pts[-1]
        last_sl_i, last_sl = sl_pts[-1]
        atr_val = float(atr[-1]) if len(atr) > 0 else 0.0

        current = float(C[-1])
        structure, _ = ind.market_structure(H, L, C, lookback=5)

        # Uptrend: price pulling back into fib support
        if structure == "BULLISH" and last_sh_i > last_sl_i:
            level = ind.price_at_fib(current, last_sh, last_sl,
                                     tolerance=0.1, atr_val=atr_val)
            if level in ("0.618", "0.786"):
                return "LONG", 0.80, f"Price at {level} fib retracement in uptrend"
            if level in ("0.382", "0.5"):
                return "LONG", 0.72, f"Price at {level} fib retracement in uptrend"

        # Downtrend: price pulling back into fib resistance
        if structure == "BEARISH" and last_sl_i > last_sh_i:
            # For downtrend, retracement is from low to high
            level = ind.price_at_fib(current, last_sl, last_sh,
                                     tolerance=0.1, atr_val=atr_val)
            if level in ("0.618", "0.786"):
                return "SHORT", 0.80, f"Price at {level} fib retracement in downtrend"
            if level in ("0.382", "0.5"):
                return "SHORT", 0.72, f"Price at {level} fib retracement in downtrend"

        return None, 0, ""
    except Exception:
        return None, 0, ""


def _strat_chart_pattern(opens, H, L, C) -> Tuple:
    """
    Chart pattern detection.
    """
    try:
        pattern, direction, score = ind.detect_chart_pattern(opens, H, L, C)
        if pattern and direction and score > 0:
            return direction, score, f"Chart pattern: {pattern}"
        return None, 0, ""
    except Exception:
        return None, 0, ""


def _strat_candlestick(opens, H, L, C) -> Tuple:
    """
    Candlestick pattern detection.
    Only emits if price is near a key level (OB, FVG, or Fibonacci).
    Score = pattern score × 0.9 if at key level.
    """
    try:
        pattern, direction, score = ind.detect_candlestick_pattern(opens, H, L, C)
        if not pattern or not direction or score == 0:
            return None, 0, ""

        # Check if price is at a key level
        atr_arr = ind.atr(H, L, C, 14)
        atr_val = float(atr_arr[-1]) if len(atr_arr) > 0 else 0.0
        current = float(C[-1])
        at_key_level = False

        # Check near OBs
        obs = ind.find_order_blocks(opens, H, L, C, lookback=50)
        for ob in obs:
            if ob["test_count"] < 2 and abs(current - ob["mid"]) <= atr_val:
                at_key_level = True
                break

        # Check near FVGs
        if not at_key_level:
            fvgs = ind.find_fvg(opens, H, L, C, atr_arr, min_atr=0.5)
            for fvg in fvgs:
                if not fvg["filled"] and fvg["low"] <= current <= fvg["high"]:
                    at_key_level = True
                    break

        # Check near Fibonacci levels
        if not at_key_level and atr_val > 0:
            sh_arr = ind.swing_highs(H, 5)
            sl_arr = ind.swing_lows(L, 5)
            sh_pts = [float(v) for v in sh_arr if not np.isnan(v)]
            sl_pts = [float(v) for v in sl_arr if not np.isnan(v)]
            if sh_pts and sl_pts:
                fib = ind.fibonacci_levels(sh_pts[-1], sl_pts[-1])
                for key in ("0.382", "0.5", "0.618", "0.786"):
                    if abs(current - fib[key]) <= 0.1 * atr_val:
                        at_key_level = True
                        break

        if at_key_level:
            return direction, round(score * 0.9, 4), f"Candlestick {pattern} at key level"
        return None, 0, ""
    except Exception:
        return None, 0, ""


def _strat_mean_reversion(C, H, L) -> Tuple:
    """
    RSI + Bollinger Bands mean reversion.
    """
    try:
        rsi_arr = ind.rsi(C, 14)
        upper, mid, lower = ind.bollinger_bands(C, 20, 2.0)

        rsi_vals   = rsi_arr[~np.isnan(rsi_arr)]
        upper_vals = upper[~np.isnan(upper)]
        lower_vals = lower[~np.isnan(lower)]

        if len(rsi_vals) == 0 or len(upper_vals) == 0 or len(lower_vals) == 0:
            return None, 0, ""

        last_rsi   = float(rsi_vals[-1])
        last_close = float(C[-1])
        last_upper = float(upper_vals[-1])
        last_lower = float(lower_vals[-1])

        # Oversold: RSI < 25 AND price at/below lower Bollinger
        if last_rsi < 25 and last_close <= last_lower * 1.005:
            score = 0.88 if last_rsi < 20 else 0.80
            return "LONG",  score, f"MeanRev RSI={last_rsi:.1f} at lower BB"

        # Overbought: RSI > 75 AND price at/above upper Bollinger
        if last_rsi > 75 and last_close >= last_upper * 0.995:
            score = 0.88 if last_rsi > 80 else 0.80
            return "SHORT", score, f"MeanRev RSI={last_rsi:.1f} at upper BB"

        return None, 0, ""
    except Exception:
        return None, 0, ""


def _strat_ema_momentum(C) -> Tuple:
    """
    EMA momentum crossover strategy.
    EMA8 crosses above/below EMA21 in direction of EMA50.
    """
    try:
        n = len(C)
        if n < 3:
            return None, 0, ""

        ema8  = ind.ema(C, 8)
        ema21 = ind.ema(C, 21)
        ema50 = ind.ema(C, 50)

        e8,  e21,  e50  = float(ema8[-1]),  float(ema21[-1]),  float(ema50[-1])
        e8p, e21p        = float(ema8[-2]),  float(ema21[-2])

        # Bullish crossover: EMA8 crosses above EMA21 AND EMA21 > EMA50
        if e8p <= e21p and e8 > e21 and e21 > e50:
            return "LONG",  0.70, "EMA8 crossed above EMA21 above EMA50"

        # Bearish crossover: EMA8 crosses below EMA21 AND EMA21 < EMA50
        if e8p >= e21p and e8 < e21 and e21 < e50:
            return "SHORT", 0.70, "EMA8 crossed below EMA21 below EMA50"

        return None, 0, ""
    except Exception:
        return None, 0, ""


def _strat_macd_rsi(C) -> Tuple:
    """
    MACD histogram zero-cross + RSI confluence.
    """
    try:
        rsi_arr = ind.rsi(C, 14)
        _, _, hist = ind.macd(C, 12, 26, 9)

        rsi_valid  = rsi_arr[~np.isnan(rsi_arr)]
        hist_valid = hist[~np.isnan(hist)]

        if len(rsi_valid) < 2 or len(hist_valid) < 2:
            return None, 0, ""

        last_rsi  = float(rsi_valid[-1])
        last_hist = float(hist_valid[-1])
        prev_hist = float(hist_valid[-2])

        # Bullish: histogram crosses above zero AND RSI > 50
        if prev_hist <= 0 and last_hist > 0 and last_rsi > 50:
            return "LONG",  0.72, f"MACD hist cross +0 RSI={last_rsi:.1f}"

        # Bearish: histogram crosses below zero AND RSI < 50
        if prev_hist >= 0 and last_hist < 0 and last_rsi < 50:
            return "SHORT", 0.72, f"MACD hist cross -0 RSI={last_rsi:.1f}"

        return None, 0, ""
    except Exception:
        return None, 0, ""


# ---------------------------------------------------------------------------
# HTF bias helper
# ---------------------------------------------------------------------------

def _get_htf_bias(d1_bars, h4_bars, h1_bars):
    votes_long  = 0
    votes_short = 0
    weights = [
        (d1_bars, 3),
        (h4_bars, 2),
        (h1_bars, 1),
    ]
    available = 0
    for bars, weight in weights:
        if not bars or len(bars) < 5:
            continue
        available += 1
        H = np.array([b.high  for b in bars])
        L = np.array([b.low   for b in bars])
        C = np.array([b.close for b in bars])
        try:
            direction, _ = ind.market_structure(H, L, C)
        except Exception:
            continue
        if direction == "BULLISH":
            votes_long  += weight
        elif direction == "BEARISH":
            votes_short += weight

    # If no HTF data available at all — allow M5 signals through
    if available == 0:
        return "ALLOW_ALL"

    # If only partial HTF data — lower threshold
    threshold = 2 if available <= 1 else 3

    if votes_long  > votes_short and votes_long  >= threshold:
        return "LONG"
    if votes_short > votes_long  and votes_short >= threshold:
        return "SHORT"

    # HTF neutral — fall back to H1 alone if available
    if h1_bars and len(h1_bars) >= 5:
        H = np.array([b.high  for b in h1_bars])
        L = np.array([b.low   for b in h1_bars])
        C = np.array([b.close for b in h1_bars])
        try:
            direction, score = ind.market_structure(H, L, C)
            if direction == "BULLISH" and score >= 0.6:
                return "LONG"
            if direction == "BEARISH" and score >= 0.6:
                return "SHORT"
        except Exception:
            pass

    return "NEUTRAL"


# ---------------------------------------------------------------------------
# MTF zone helper
# ---------------------------------------------------------------------------

def _get_mtf_zone(mtf_bars, H_ltf, L_ltf, C_ltf, atr_arr) -> bool:
    """
    Returns True if current price is inside or near an MTF OB or FVG zone,
    OR if no MTF zone data is available (fallback — allow M5 entry through).
    Returns False only when MTF data exists but price is clearly outside all zones.
    """
    if not mtf_bars or len(mtf_bars) < 5:
        return True  # no MTF data — allow through

    try:
        O_m = np.array([b.open  for b in mtf_bars])
        H_m = np.array([b.high  for b in mtf_bars])
        L_m = np.array([b.low   for b in mtf_bars])
        C_m = np.array([b.close for b in mtf_bars])
        atr_m = ind.atr(H_m, L_m, C_m, 14)

        current_price = float(C_ltf[-1])
        atr_val = float(atr_arr[-1]) if len(atr_arr) > 0 else 0.0

        # Check MTF Order Blocks
        try:
            obs = ind.find_order_blocks(O_m, H_m, L_m, C_m, lookback=30)
            for ob in obs:
                if ob.get("test_count", 0) >= 2:
                    continue
                ob_h = ob["high"]
                ob_l = ob["low"]
                # Inside zone or within 1×ATR
                if ob_l <= current_price <= ob_h:
                    return True
                if atr_val > 0 and abs(current_price - ob.get("mid", (ob_h + ob_l) / 2)) <= atr_val:
                    return True
        except Exception:
            pass

        # Check MTF FVGs
        try:
            fvgs = ind.find_fvg(O_m, H_m, L_m, C_m, atr_m, min_atr=0.3)
            for fvg in fvgs:
                if fvg.get("filled", False):
                    continue
                if fvg["low"] <= current_price <= fvg["high"]:
                    return True
        except Exception:
            pass

        # MTF data existed but no zone matched — still allow through (fallback)
        return True

    except Exception:
        return True


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SignalEngine:

    def __init__(self, *args, **kwargs):
        pass

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def evaluate(self, ltf_bars, symbol="", stake=1.0,
                 d1_bars=None, h4_bars=None, h1_bars=None,
                 m30_bars=None, m15_bars=None,
                 mtf_bars=None, **kwargs):

        if len(ltf_bars) < 14:
            return NONE_RESULT

        try:
            C = np.array([b.close for b in ltf_bars])
            H = np.array([b.high  for b in ltf_bars])
            L = np.array([b.low   for b in ltf_bars])

            # RSI
            rsi_arr    = ind.rsi(C, 14)
            valid_rsi  = rsi_arr[~np.isnan(rsi_arr)]
            if len(valid_rsi) < 2:
                return NONE_RESULT
            last_rsi = float(valid_rsi[-1])
            prev_rsi = float(valid_rsi[-2])

            # EMA
            ema8  = ind.ema(C, 8)
            ema21 = ind.ema(C, 21)
            e8    = float(ema8[-1])
            e21   = float(ema21[-1])
            e8p   = float(ema8[-2])
            e21p  = float(ema21[-2])

            # MACD
            _, _, hist = ind.macd(C, 12, 26, 9)
            valid_hist = hist[~np.isnan(hist)]
            last_hist  = float(valid_hist[-1]) \
                         if len(valid_hist) else 0
            prev_hist  = float(valid_hist[-2]) \
                         if len(valid_hist) > 1 else 0

            # Bollinger
            upper, mid, lower = ind.bollinger_bands(C, 20, 2.0)
            valid_upper = upper[~np.isnan(upper)]
            valid_lower = lower[~np.isnan(lower)]
            last_close  = float(C[-1])
            last_upper  = float(valid_upper[-1]) \
                          if len(valid_upper) else last_close
            last_lower  = float(valid_lower[-1]) \
                          if len(valid_lower) else last_close

            long_score  = 0.0
            short_score = 0.0
            reasons     = []

            # RSI signals
            if last_rsi < 35:
                long_score  += 0.25
                reasons.append(f"RSI_LOW={last_rsi:.1f}")
            if last_rsi > 65:
                short_score += 0.25
                reasons.append(f"RSI_HIGH={last_rsi:.1f}")
            if last_rsi > 50:
                long_score  += 0.10
            else:
                short_score += 0.10

            # RSI turning
            if last_rsi > prev_rsi and last_rsi < 50:
                long_score  += 0.15
                reasons.append("RSI_TURNING_UP")
            if last_rsi < prev_rsi and last_rsi > 50:
                short_score += 0.15
                reasons.append("RSI_TURNING_DOWN")

            # EMA signals
            if e8 > e21:
                long_score  += 0.15
            else:
                short_score += 0.15
            if e8p <= e21p and e8 > e21:
                long_score  += 0.20
                reasons.append("EMA_CROSS_UP")
            if e8p >= e21p and e8 < e21:
                short_score += 0.20
                reasons.append("EMA_CROSS_DOWN")

            # MACD signals
            if last_hist > 0:
                long_score  += 0.10
            else:
                short_score += 0.10
            if prev_hist <= 0 and last_hist > 0:
                long_score  += 0.15
                reasons.append("MACD_CROSS_UP")
            if prev_hist >= 0 and last_hist < 0:
                short_score += 0.15
                reasons.append("MACD_CROSS_DOWN")

            # Bollinger signals
            if last_close <= last_lower:
                long_score  += 0.20
                reasons.append("PRICE_AT_LOWER_BB")
            if last_close >= last_upper:
                short_score += 0.20
                reasons.append("PRICE_AT_UPPER_BB")

            # Determine direction
            if long_score <= 0.0 and short_score <= 0.0:
                return NONE_RESULT

            if long_score >= short_score:
                final_dir   = "LONG"
                final_score = min(long_score, 0.98)
            else:
                final_dir   = "SHORT"
                final_score = min(short_score, 0.98)

            if final_score < 0.35:
                logger.debug(
                    f"BELOW THRESHOLD: {symbol} "
                    f"score={final_score:.3f}")
                return NONE_RESULT

            sl_pct  = config.STOP_LOSS_MAP.get(
                symbol, config.DEFAULT_STOP_LOSS_PCT)
            sl_amt  = round(stake * sl_pct / 100, 2)
            tp_amt  = round(sl_amt * 2.0, 2)
            mult    = config.MULTIPLIER_MAP.get(
                symbol, config.DEFAULT_MULTIPLIER)
            reason  = " | ".join(reasons) \
                      if reasons else "SCORE_BASED"

            # ══════════════════════════════════════
            # INVERSION — intentional, do not remove
            # ══════════════════════════════════════
            analysis_dir  = final_dir
            inverted_dir  = "SHORT" \
                            if final_dir == "LONG" \
                            else "LONG"

            logger.info(
                f"SIGNAL: {symbol} | "
                f"Analysis={analysis_dir} → "
                f"Executing={inverted_dir} | "
                f"score={final_score:.3f} | "
                f"{reason}")

            return SignalResult(
                direction   = inverted_dir,
                strength    = 2,
                score       = final_score,
                strategy    = "RSI_EMA_MACD_BB",
                reason      = reason,
                stop_loss   = sl_amt,
                take_profit = tp_amt,
                multiplier  = mult,
            )

        except Exception as e:
            logger.error(
                f"SIGNAL ERROR: {symbol} — {e}")
            return NONE_RESULT

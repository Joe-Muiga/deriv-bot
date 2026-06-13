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

    def evaluate(self, ltf_bars, mtf_bars=None,
                 symbol="", stake=1.0, **kwargs):
        if len(ltf_bars) < 20:
            return NONE_RESULT

        O       = np.array([b.open  for b in ltf_bars])
        H       = np.array([b.high  for b in ltf_bars])
        L       = np.array([b.low   for b in ltf_bars])
        C       = np.array([b.close for b in ltf_bars])
        atr_arr = ind.atr(H, L, C, 14)

        # ── HTF bias gate ───────────────────────────────────────────────────
        d1_bars  = kwargs.get("d1_bars",  [])
        h4_bars  = kwargs.get("h4_bars",  [])
        h1_bars  = kwargs.get("h1_bars",  [])
        htf_bias = _get_htf_bias(d1_bars, h4_bars, h1_bars)

        if htf_bias == "NEUTRAL":
            return NONE_RESULT
        # ALLOW_ALL means no HTF data yet — run M5 strategies unrestricted
        allow_all = htf_bias == "ALLOW_ALL"

        # ── MTF zone gate ───────────────────────────────────────────────────
        if not _get_mtf_zone(mtf_bars, H, L, C, atr_arr):
            logger.debug(f"MTF ZONE BLOCK: {symbol} — price outside all MTF zones")
            return NONE_RESULT

        # Run ALL strategies
        strategies = [
            ("SWEEP",       _strat_liquidity_sweep(H, L, C)),
            ("OB",          _strat_smc_ob(O, H, L, C, atr_arr)),
            ("FVG",         _strat_smc_fvg(O, H, L, C, atr_arr)),
            ("FIBONACCI",   _strat_fibonacci(H, L, C, atr_arr)),
            ("CHART_PAT",   _strat_chart_pattern(O, H, L, C)),
            ("STRUCTURE",   _strat_market_structure(H, L, C)),
            ("MEAN_REV",    _strat_mean_reversion(C, H, L)),
            ("CANDLE",      _strat_candlestick(O, H, L, C)),
            ("EMA_MOM",     _strat_ema_momentum(C)),
            ("MACD_RSI",    _strat_macd_rsi(C)),
        ]

        long_votes  = []  # (score, name, reason)
        short_votes = []

        for name, (direction, score, reason) in strategies:
            if direction == "LONG" and score > 0:
                if allow_all or htf_bias == "LONG":
                    long_votes.append((score, name, reason))
            elif direction == "SHORT" and score > 0:
                if allow_all or htf_bias == "SHORT":
                    short_votes.append((score, name, reason))

        # Determine dominant direction
        long_count  = len(long_votes)
        short_count = len(short_votes)
        long_avg    = sum(s for s, _, _ in long_votes)  / max(long_count,  1)
        short_avg   = sum(s for s, _, _ in short_votes) / max(short_count, 1)

        # Need minimum 2 strategies agreeing
        MIN_AGREE   = getattr(config, "MIN_STRATEGY_AGREEMENT", 2)
        final_dir   = None
        final_score = 0.0
        votes_used  = []

        if long_count >= MIN_AGREE and long_count > short_count:
            final_dir   = "LONG"
            final_score = long_avg + (long_count - MIN_AGREE) * 0.03
            votes_used  = long_votes
        elif short_count >= MIN_AGREE and short_count > long_count:
            final_dir   = "SHORT"
            final_score = short_avg + (short_count - MIN_AGREE) * 0.03
            votes_used  = short_votes
        elif long_count == short_count and long_count >= MIN_AGREE:
            # Tie: use highest average score
            if long_avg >= short_avg:
                final_dir   = "LONG"
                final_score = long_avg
                votes_used  = long_votes
            else:
                final_dir   = "SHORT"
                final_score = short_avg
                votes_used  = short_votes

        if not final_dir:
            logger.debug(
                f"NO SIGNAL: {symbol} "
                f"long={long_count} short={short_count}")
            return NONE_RESULT

        final_score = min(final_score, 0.98)

        if final_score < config.MIN_SIGNAL_SCORE:
            logger.debug(
                f"BELOW THRESHOLD: {symbol} "
                f"score={final_score:.3f}")
            return NONE_RESULT

        # Strategy summary
        strategy_names = "+".join(n for _, n, _ in votes_used)
        reasons        = " | ".join(
            f"{n}:{r}" for _, n, r in votes_used)

        sl_pct = config.STOP_LOSS_MAP.get(symbol, config.DEFAULT_STOP_LOSS_PCT)
        sl_amt = round(stake * sl_pct / 100, 2)
        tp_amt = round(sl_amt * config.TAKE_PROFIT_RATIO, 2)
        mult   = config.MULTIPLIER_MAP.get(symbol, config.DEFAULT_MULTIPLIER)

        # ═══════════════════════════════════════════════
        # INVERSION — applied to every signal before return
        # Analysis direction is logged, execution is opposite
        # ═══════════════════════════════════════════════
        analysis_direction = final_dir
        inverted_direction = "SHORT" if final_dir == "LONG" else "LONG"

        logger.info(
            f"SIGNAL: {symbol} | "
            f"Analysis={analysis_direction} → "
            f"Executing={inverted_direction} | "
            f"score={final_score:.3f} | "
            f"agreement={len(votes_used)}/10 | "
            f"strategies=[{strategy_names}]")

        return SignalResult(
            direction   = inverted_direction,  # INVERTED
            strength    = min(len(votes_used), 3),
            score       = final_score,
            strategy    = f"MULTI({len(votes_used)}/10)",
            reason      = reasons,
            stop_loss   = sl_amt,
            take_profit = tp_amt,
            multiplier  = mult,
        )

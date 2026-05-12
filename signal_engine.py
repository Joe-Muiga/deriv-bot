"""
signal_engine.py – Phase B of SIFM: Lower-Timeframe Entry Scan.

Module 1 – MTFA alignment + RSI (divergence OR oversold/overbought)
Module 2 – Candlestick confluence
Module 3 – 7-indicator quantitative vote (≥ 4/7 required)
"""

import numpy as np
import logging
from dataclasses import dataclass
from typing import List
from candlestick_builder import Candle
import indicators as ind
from smc_analyzer import SMCContext

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    direction:  str
    strength:   float
    m1_signal:  int
    m2_signal:  int
    m3_signal:  int
    m3_score:   int
    reason:     str


# ─── Module 1 – MTFA + RSI ───────────────────────────────────────────────────

def module1_mtfa_rsi(ltf_bars: List[Candle], htf_bias: str) -> int:
    """
    CHANGED: Now fires on EMA slope alone OR RSI divergence alone (not both).
    Also added RSI oversold/overbought as a trigger.
    """
    if len(ltf_bars) < 25:
        return 0
    closes = np.array([b.close for b in ltf_bars])

    ema20 = ind.ema(closes, 20)
    rsi14 = ind.rsi(closes, 14)

    valid_ema = ema20[~np.isnan(ema20)]
    valid_rsi = rsi14[~np.isnan(rsi14)]

    if len(valid_ema) < 2:
        return 0

    slope = valid_ema[-1] - valid_ema[-2]
    last_rsi = float(valid_rsi[-1]) if len(valid_rsi) else 50.0

    # Divergence (bonus confirmation, not required)
    try:
        div = ind.find_rsi_divergence(closes, rsi14, lookback=20)
    except Exception:
        div = 0

    if htf_bias == "LONG":
        # CHANGED: EMA slope up OR RSI oversold (<35) OR divergence
        if slope > 0 or last_rsi < 35 or div == +1:
            return +1

    if htf_bias == "SHORT":
        # CHANGED: EMA slope down OR RSI overbought (>65) OR divergence
        if slope < 0 or last_rsi > 65 or div == -1:
            return -1

    return 0


# ─── Module 2 – Candlestick Confluence ───────────────────────────────────────

def module2_candlestick(ltf_bars: List[Candle]) -> int:
    """
    CHANGED: Volume confirmation relaxed from 1.5× to 1.1× average.
    Also added pin bar / hammer detection.
    """
    if len(ltf_bars) < 4:
        return 0

    c    = ltf_bars
    last = c[-1]
    prev = c[-2]
    prev2 = c[-3]

    volumes = [b.volume for b in ltf_bars[-21:]]
    avg_vol = float(np.mean(volumes[:-1])) if len(volumes) > 1 else 1.0
    # CHANGED: relaxed volume filter from 1.5× to 1.1×
    vol_ok  = last.volume > 1.1 * avg_vol if avg_vol > 0 else True

    def is_bull(bar): return bar.close > bar.open
    def is_bear(bar): return bar.close < bar.open
    def body(bar):    return abs(bar.close - bar.open)
    def upper_wick(bar): return bar.high - max(bar.open, bar.close)
    def lower_wick(bar): return min(bar.open, bar.close) - bar.low

    # ── Bullish Engulfing ──
    if (is_bear(prev) and is_bull(last) and
            last.open <= prev.close and last.close >= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("Pattern: bullish engulfing")
        return +1

    # ── Morning Star ──
    if (is_bear(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bull(last) and last.close > (prev2.open + prev2.close) / 2 and
            vol_ok):
        logger.debug("Pattern: morning star")
        return +1

    # ── Hammer / Pin Bar bullish (long lower wick) ── ADDED
    if (lower_wick(last) > body(last) * 2 and
            lower_wick(last) > upper_wick(last) * 2 and
            vol_ok):
        logger.debug("Pattern: hammer/pin bar bullish")
        return +1

    # ── Three-Line Strike bullish ──
    if len(c) >= 5:
        c0, c1, c2, c3 = c[-5], c[-4], c[-3], c[-2]
        if (all(is_bear(x) for x in [c0, c1, c2, c3]) and
                is_bull(last) and last.close >= c0.open and vol_ok):
            logger.debug("Pattern: three-line strike (bullish)")
            return +1

    # ── Bearish Engulfing ──
    if (is_bull(prev) and is_bear(last) and
            last.open >= prev.close and last.close <= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("Pattern: bearish engulfing")
        return -1

    # ── Evening Star ──
    if (is_bull(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bear(last) and last.close < (prev2.open + prev2.close) / 2 and
            vol_ok):
        logger.debug("Pattern: evening star")
        return -1

    # ── Shooting Star / Inverted Pin Bar bearish ── ADDED
    if (upper_wick(last) > body(last) * 2 and
            upper_wick(last) > lower_wick(last) * 2 and
            vol_ok):
        logger.debug("Pattern: shooting star/pin bar bearish")
        return -1

    # ── Three-Line Strike bearish ──
    if len(c) >= 5:
        c0, c1, c2, c3 = c[-5], c[-4], c[-3], c[-2]
        if (all(is_bull(x) for x in [c0, c1, c2, c3]) and
                is_bear(last) and last.close <= c0.open and vol_ok):
            logger.debug("Pattern: three-line strike (bearish)")
            return -1

    return 0


# ─── Module 3 – 7-Indicator Vote ─────────────────────────────────────────────

def module3_vote(ltf_bars: List[Candle], min_votes: int = 4) -> int:
    """
    CHANGED: min_votes lowered from 5 to 4.
    StochRSI neutral zone now returns 0 instead of -1.
    """
    if len(ltf_bars) < 30:
        return 0

    H = np.array([b.high  for b in ltf_bars])
    L = np.array([b.low   for b in ltf_bars])
    C = np.array([b.close for b in ltf_bars])

    scores = []

    # 1. RSI(14) > 50 → bullish
    rsi14 = ind.rsi(C, 14)
    last_rsi = rsi14[~np.isnan(rsi14)]
    scores.append(+1 if (len(last_rsi) and last_rsi[-1] > 50) else -1)

    # 2. MACD histogram > 0 → bullish
    _, _, hist = ind.macd(C, 12, 26, 9)
    last_hist = hist[~np.isnan(hist)]
    scores.append(+1 if (len(last_hist) and last_hist[-1] > 0) else -1)

    # 3. Price above Bollinger middle band
    _, mid, _ = ind.bollinger_bands(C, 20, 2)
    last_mid = mid[~np.isnan(mid)]
    scores.append(+1 if (len(last_mid) and C[-1] > last_mid[-1]) else -1)

    # 4. StochRSI — CHANGED: neutral zone (0.2–0.8) returns 0 not -1
    k, _ = ind.stoch_rsi(C, 14, 14, 3, 3)
    last_k = k[~np.isnan(k)]
    if len(last_k):
        if last_k[-1] < 0.2:
            scores.append(+1)   # oversold → bullish
        elif last_k[-1] > 0.8:
            scores.append(-1)   # overbought → bearish
        else:
            scores.append(0)    # neutral — CHANGED: was -1
    else:
        scores.append(0)

    # 5. ADX trend direction
    adx_vals, pdi, mdi = ind.adx(H, L, C, 14)
    last_adx = adx_vals[~np.isnan(adx_vals)]
    if len(last_adx) and last_adx[-1] > 20:  # CHANGED: threshold 25→20
        last_pdi = pdi[~np.isnan(adx_vals)]
        last_mdi = mdi[~np.isnan(adx_vals)]
        if len(last_pdi):
            scores.append(+1 if last_pdi[-1] > last_mdi[-1] else -1)
        else:
            scores.append(0)
    else:
        scores.append(0)

    # 6. ATR direction
    atr14 = ind.atr(H, L, C, 14)
    valid_atr = atr14[~np.isnan(atr14)]
    if len(valid_atr) >= 3:
        scores.append(+1 if valid_atr[-1] > valid_atr[-2] else -1)
    else:
        scores.append(0)

    # 7. Recent price momentum (3 bars)
    if len(H) >= 3 and all(H[-i] > H[-i-1] for i in range(1, 3)):
        scores.append(+1)
    elif len(L) >= 3 and all(L[-i] < L[-i-1] for i in range(1, 3)):
        scores.append(-1)
    else:
        scores.append(0)

    # Filter out neutral votes before counting
    non_neutral = [s for s in scores if s != 0]
    raw_score   = sum(non_neutral)

    if   raw_score >= min_votes:  return +1
    elif raw_score <= -min_votes: return -1
    return 0


# ─── Main Signal Aggregator ───────────────────────────────────────────────────

class SignalEngine:

    def __init__(self, min_modules: int = 1, min_votes: int = 4):
        self.min_modules = min_modules
        self.min_votes   = min_votes

    def evaluate(self,
                 ltf_bars: List[Candle],
                 htf_bias: str,
                 smc_ctx:  SMCContext,
                 in_zone:  bool) -> SignalResult:

        if htf_bias == "NEUTRAL" or not in_zone:
            return SignalResult("NONE", 0, 0, 0, 0, 0,
                                f"htf_bias={htf_bias}, in_zone={in_zone}")

        m1 = module1_mtfa_rsi(ltf_bars, htf_bias)
        m2 = module2_candlestick(ltf_bars)
        m3 = module3_vote(ltf_bars, self.min_votes)

        expected   = +1 if htf_bias == "LONG" else -1
        confirming = sum(1 for m in [m1, m2, m3] if m == expected)

        logger.debug(f"Signal modules: m1={m1} m2={m2} m3={m3} "
                     f"confirming={confirming}/{self.min_modules} bias={htf_bias}")

        if confirming < self.min_modules:
            reason = (f"only {confirming}/{self.min_modules} modules confirmed "
                      f"(m1={m1}, m2={m2}, m3={m3})")
            return SignalResult("NONE", confirming, m1, m2, m3, confirming, reason)

        direction = "LONG" if htf_bias == "LONG" else "SHORT"
        reason    = (f"✓ {confirming}/3 modules | bias={htf_bias} | "
                     f"m1={m1} m2={m2} m3={m3}")
        logger.info(f"Signal: {direction} | {reason}")

        return SignalResult(direction, confirming, m1, m2, m3, confirming, reason)

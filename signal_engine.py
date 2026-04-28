"""
signal_engine.py – Phase B of SIFM: Lower-Timeframe Entry Scan.

Implements the three confirmation modules and produces a final
LONG / SHORT / NONE signal.

Module 1 – MTFA alignment + RSI divergence
Module 2 – Candlestick confluence (high-reliability patterns, volume-confirmed)
Module 3 – 7-indicator quantitative vote (≥ 5 / 7 required)

Final entry: bias + in-SMC-zone + ≥ 2/3 modules agree.
"""

import numpy as np
import logging
from dataclasses import dataclass
from typing import List, Optional
from candlestick_builder import Candle
import indicators as ind
from smc_analyzer import SMCContext

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    direction:  str    # "LONG" | "SHORT" | "NONE"
    strength:   float  # 0–3 (number of confirming modules)
    m1_signal:  int    # +1 / 0 / -1
    m2_signal:  int
    m3_signal:  int
    m3_score:   int    # how many of 7 indicators agreed
    reason:     str


# ─────────────────────────────────────────────────────────────────────────────
# Module 1 – MTFA + RSI Divergence
# ─────────────────────────────────────────────────────────────────────────────

def module1_mtfa_rsi(ltf_bars: List[Candle], htf_bias: str) -> int:
    """
    Returns +1 (bullish), -1 (bearish), 0 (neutral/no signal).

    Conditions:
      LONG:  LTF 20-EMA slope > 0  AND  RSI bullish divergence detected
      SHORT: LTF 20-EMA slope < 0  AND  RSI bearish divergence detected
    """
    if len(ltf_bars) < 25:
        return 0
    closes = np.array([b.close for b in ltf_bars])

    ema20    = ind.ema(closes, 20)
    rsi14    = ind.rsi(closes, 14)
    div      = ind.find_rsi_divergence(closes, rsi14, lookback=20)

    # EMA slope: compare last two valid EMA values
    valid_ema = ema20[~np.isnan(ema20)]
    if len(valid_ema) < 2:
        return 0
    slope = valid_ema[-1] - valid_ema[-2]

    if htf_bias == "LONG"  and slope > 0 and div == +1:
        return +1
    if htf_bias == "SHORT" and slope < 0 and div == -1:
        return -1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Module 2 – Candlestick Confluence (Bulkowski high-reliability patterns)
# ─────────────────────────────────────────────────────────────────────────────

def module2_candlestick(ltf_bars: List[Candle]) -> int:
    """
    Detects: bullish engulfing, morning star, three-line strike (bullish);
    and their bearish equivalents.

    Volume confirmation: current bar volume > 1.5 × 20-bar average.
    Returns +1 / -1 / 0.
    """
    if len(ltf_bars) < 4:
        return 0

    c = ltf_bars
    last = c[-1]
    prev = c[-2]
    prev2 = c[-3]

    volumes = [b.volume for b in ltf_bars[-21:]]
    avg_vol = float(np.mean(volumes[:-1])) if len(volumes) > 1 else 1.0
    vol_ok  = last.volume > 1.5 * avg_vol if avg_vol > 0 else True

    # Helper booleans
    def is_bull(bar): return bar.close > bar.open
    def is_bear(bar): return bar.close < bar.open
    def body(bar):    return abs(bar.close - bar.open)

    # ── Bullish Engulfing ──
    if (is_bear(prev) and is_bull(last) and
            last.open <= prev.close and last.close >= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("Pattern: bullish engulfing")
        return +1

    # ── Morning Star (3-candle) ──
    if (is_bear(prev2) and body(prev) < body(prev2) * 0.5 and  # small middle body
            is_bull(last) and last.close > (prev2.open + prev2.close) / 2 and
            vol_ok):
        logger.debug("Pattern: morning star")
        return +1

    # ── Three-Line Strike bullish (3 bearish + 1 engulfing bull) ──
    if len(c) >= 5:
        c0, c1, c2, c3 = c[-5], c[-4], c[-3], c[-2]
        if (all(is_bear(x) for x in [c0, c1, c2, c3]) and
                is_bull(last) and
                last.close >= c0.open and vol_ok):
            logger.debug("Pattern: three-line strike (bullish)")
            return +1

    # ── Bearish Engulfing ──
    if (is_bull(prev) and is_bear(last) and
            last.open >= prev.close and last.close <= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("Pattern: bearish engulfing")
        return -1

    # ── Evening Star (3-candle) ──
    if (is_bull(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bear(last) and last.close < (prev2.open + prev2.close) / 2 and
            vol_ok):
        logger.debug("Pattern: evening star")
        return -1

    # ── Three-Line Strike bearish ──
    if len(c) >= 5:
        c0, c1, c2, c3 = c[-5], c[-4], c[-3], c[-2]
        if (all(is_bull(x) for x in [c0, c1, c2, c3]) and
                is_bear(last) and
                last.close <= c0.open and vol_ok):
            logger.debug("Pattern: three-line strike (bearish)")
            return -1

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Module 3 – 7-Indicator Quantitative Vote
# ─────────────────────────────────────────────────────────────────────────────

def module3_vote(ltf_bars: List[Candle], min_votes: int = 5) -> int:
    """
    Tallies 7 indicators; each casts +1 (bullish) or -1 (bearish).
    Returns +1 if score ≥ min_votes, -1 if score ≤ -min_votes, else 0.
    Also returns the raw score as second value.
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

    # 4. StochRSI < 0.2 (oversold → bullish reversal potential)
    k, _ = ind.stoch_rsi(C, 14, 14, 3, 3)
    last_k = k[~np.isnan(k)]
    if len(last_k):
        scores.append(+1 if last_k[-1] < 0.2 else (-1 if last_k[-1] > 0.8 else 0))
    else:
        scores.append(0)

    # 5. ADX > 25 (trending)
    adx_vals, pdi, mdi = ind.adx(H, L, C, 14)
    last_adx = adx_vals[~np.isnan(adx_vals)]
    if len(last_adx) and last_adx[-1] > 25:
        # If trending and +DI > -DI → bullish trend
        last_pdi = pdi[~np.isnan(adx_vals)]
        last_mdi = mdi[~np.isnan(adx_vals)]
        if len(last_pdi):
            scores.append(+1 if last_pdi[-1] > last_mdi[-1] else -1)
        else:
            scores.append(0)
    else:
        scores.append(0)   # not trending → neutral

    # 6. ATR rising (volatility expansion) → supports the move
    atr14 = ind.atr(H, L, C, 14)
    valid_atr = atr14[~np.isnan(atr14)]
    if len(valid_atr) >= 3:
        scores.append(+1 if valid_atr[-1] > valid_atr[-2] else -1)
    else:
        scores.append(0)

    # 7. Price action: last 3 bars higher highs (bullish) or lower lows (bearish)
    if len(H) >= 3 and all(H[-i] > H[-i-1] for i in range(1,3)):
        scores.append(+1)
    elif len(L) >= 3 and all(L[-i] < L[-i-1] for i in range(1,3)):
        scores.append(-1)
    else:
        scores.append(0)

    raw_score = sum(scores)
    if   raw_score >= min_votes:  return +1
    elif raw_score <= -min_votes: return -1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Main signal aggregator (Phases A + B)
# ─────────────────────────────────────────────────────────────────────────────

class SignalEngine:

    def __init__(self,
                 min_modules: int = 2,
                 min_votes:   int = 5):
        self.min_modules = min_modules
        self.min_votes   = min_votes

    def evaluate(self,
                 ltf_bars:  List[Candle],
                 htf_bias:  str,
                 smc_ctx:   SMCContext,
                 in_zone:   bool) -> SignalResult:

        if htf_bias == "NEUTRAL" or not in_zone:
            return SignalResult("NONE", 0, 0, 0, 0, 0,
                                f"htf_bias={htf_bias}, in_zone={in_zone}")

        m1 = module1_mtfa_rsi(ltf_bars, htf_bias)
        m2 = module2_candlestick(ltf_bars)
        m3_raw  = module3_vote(ltf_bars, self.min_votes)
        # m3 must agree with bias
        m3 = m3_raw

        # Count confirming modules (those that agree with the bias direction)
        expected = +1 if htf_bias == "LONG" else -1
        confirming = sum(1 for m in [m1, m2, m3] if m == expected)

        m3_score_arr: list = []  # detailed score
        # Compute m3 detailed score (rerun just for logging)
        if len(ltf_bars) >= 30:
            H = np.array([b.high  for b in ltf_bars])
            L = np.array([b.low   for b in ltf_bars])
            C = np.array([b.close for b in ltf_bars])
            rsi14 = ind.rsi(C, 14)
            last_rsi = rsi14[~np.isnan(rsi14)]
            m3_score_detail = (
                (1 if (len(last_rsi) and last_rsi[-1] > 50) else 0) +
                (1 if m3_raw == expected else 0)
            )
        else:
            m3_score_detail = 0

        if confirming < self.min_modules:
            reason = (f"only {confirming}/{self.min_modules} modules confirmed "
                      f"(m1={m1}, m2={m2}, m3={m3})")
            return SignalResult("NONE", confirming, m1, m2, m3, confirming, reason)

        direction = "LONG" if htf_bias == "LONG" else "SHORT"
        reason    = (f"✓ {confirming}/3 modules | bias={htf_bias} | "
                     f"m1={m1} m2={m2} m3={m3}")
        logger.info(f"Signal generated: {direction} | {reason}")

        return SignalResult(direction, confirming, m1, m2, m3, confirming, reason)

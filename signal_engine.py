"""
signal_engine.py – Phase B of SIFM: Lower-Timeframe Entry Scan.

Design philosophy:
  HIGH FREQUENCY: Module 1 uses dual EMA alignment (9/21) which fires on every
                  bar where the trend is intact — many signals per hour on 1M LTF.
  HIGH ACCURACY:  Requires 2/3 modules confirmed. Module 3 (7-indicator vote)
                  is the quality gate. Both must agree with HTF bias.

Module 1 – Dual EMA alignment + RSI momentum confirmation
Module 2 – Candlestick confluence (pattern confirmation)
Module 3 – 7-indicator quantitative vote (≥4/7 required)
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


# ─── Module 1 – Dual EMA Alignment + RSI Momentum ────────────────────────────

def module1_mtfa_rsi(ltf_bars: List[Candle], htf_bias: str) -> int:
    """
    Primary frequency driver.

    Uses dual EMA (9/21) crossover on the LTF — the same logic as the HTF
    structure detector but on the lower timeframe. When both HTF and LTF EMAs
    agree, the trend is strongly confirmed.

    Also checks:
    - RSI momentum: above 50 (bullish) or below 50 (bearish) — simple and reliable
    - RSI not extreme: avoids entering at overbought/oversold exhaustion
    - Divergence as a bonus signal

    Fires on EVERY bar where EMAs are aligned → high frequency in trending markets.
    """
    if len(ltf_bars) < 25:
        return 0

    closes = np.array([b.close for b in ltf_bars])

    ema9_arr  = ind.ema(closes, 9)
    ema21_arr = ind.ema(closes, 21)
    rsi14     = ind.rsi(closes, 14)

    valid9    = ema9_arr[~np.isnan(ema9_arr)]
    valid21   = ema21_arr[~np.isnan(ema21_arr)]
    valid_rsi = rsi14[~np.isnan(rsi14)]

    if len(valid9) < 3 or len(valid21) < 3:
        return 0

    e9  = valid9[-1]
    e21 = valid21[-1]
    # EMA slope over last 3 bars (sensitive to recent direction changes)
    slope9 = valid9[-1] - valid9[-3]

    last_rsi = float(valid_rsi[-1]) if len(valid_rsi) else 50.0

    try:
        div = ind.find_rsi_divergence(closes, rsi14, lookback=20)
    except Exception:
        div = 0

    if htf_bias == "LONG":
        ema_aligned = e9 > e21 and slope9 > 0          # EMA bullish alignment
        rsi_ok      = 45 < last_rsi < 75               # Momentum up, not exhausted
        if ema_aligned or rsi_ok or div == +1:
            return +1

    if htf_bias == "SHORT":
        ema_aligned = e9 < e21 and slope9 < 0          # EMA bearish alignment
        rsi_ok      = 25 < last_rsi < 55               # Momentum down, not exhausted
        if ema_aligned or rsi_ok or div == -1:
            return -1

    return 0


# ─── Module 2 – Candlestick Confluence ───────────────────────────────────────

def module2_candlestick(ltf_bars: List[Candle]) -> int:
    """
    Pattern confirmation module.
    Volume threshold: 1.1× average (relaxed for frequency).
    Includes: engulfing, morning/evening star, hammer/pin bar,
              shooting star, three-line strike, momentum candle.

    A "momentum candle" is added: a large body candle (>1.5× average body)
    in the trend direction with above-average volume. Very common on 1M charts
    during trend continuation — boosts frequency significantly.
    """
    if len(ltf_bars) < 5:
        return 0

    c    = ltf_bars
    last = c[-1]
    prev = c[-2]
    prev2 = c[-3]

    volumes = [b.volume for b in ltf_bars[-21:]]
    avg_vol = float(np.mean(volumes[:-1])) if len(volumes) > 1 else 1.0
    vol_ok  = last.volume > 1.1 * avg_vol if avg_vol > 0 else True

    bodies  = [abs(b.close - b.open) for b in ltf_bars[-21:-1]]
    avg_body = float(np.mean(bodies)) if bodies else 0.0

    def is_bull(bar):    return bar.close > bar.open
    def is_bear(bar):    return bar.close < bar.open
    def body(bar):       return abs(bar.close - bar.open)
    def upper_wick(bar): return bar.high - max(bar.open, bar.close)
    def lower_wick(bar): return min(bar.open, bar.close) - bar.low

    # ── Momentum candle (NEW: trend continuation) ──────────────────────────
    # Large bullish body > 1.5× average body with volume
    if is_bull(last) and avg_body > 0 and body(last) > avg_body * 1.5 and vol_ok:
        logger.debug("Pattern: bullish momentum candle")
        return +1
    # Large bearish body > 1.5× average body with volume
    if is_bear(last) and avg_body > 0 and body(last) > avg_body * 1.5 and vol_ok:
        logger.debug("Pattern: bearish momentum candle")
        return -1

    # ── Bullish Engulfing ──
    if (is_bear(prev) and is_bull(last) and
            last.open <= prev.close and last.close >= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("Pattern: bullish engulfing")
        return +1

    # ── Morning Star ──
    if (is_bear(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bull(last) and last.close > (prev2.open + prev2.close) / 2 and vol_ok):
        logger.debug("Pattern: morning star")
        return +1

    # ── Hammer / Pin Bar bullish ──
    if (lower_wick(last) > body(last) * 2 and
            lower_wick(last) > upper_wick(last) * 2 and vol_ok):
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
            is_bear(last) and last.close < (prev2.open + prev2.close) / 2 and vol_ok):
        logger.debug("Pattern: evening star")
        return -1

    # ── Shooting Star / Inverted Pin Bar ──
    if (upper_wick(last) > body(last) * 2 and
            upper_wick(last) > lower_wick(last) * 2 and vol_ok):
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
    Quality gate — 4/7 indicators must agree with the signal direction.
    This is the accuracy filter that prevents false signals from getting through.

    Indicators chosen for reliability on short timeframes (1M):
    1. RSI(14) direction relative to 50
    2. MACD histogram direction
    3. Price vs Bollinger middle band
    4. StochRSI — neutral zone scores 0 (not penalised)
    5. ADX trend direction (threshold 20)
    6. EMA9 vs EMA21 crossover (replaces ATR direction — more reliable)
    7. Recent price momentum (3 bars)
    """
    if len(ltf_bars) < 30:
        return 0

    H = np.array([b.high  for b in ltf_bars])
    L = np.array([b.low   for b in ltf_bars])
    C = np.array([b.close for b in ltf_bars])

    scores = []

    # 1. RSI(14) — direction of momentum
    rsi14 = ind.rsi(C, 14)
    last_rsi_arr = rsi14[~np.isnan(rsi14)]
    if len(last_rsi_arr):
        scores.append(+1 if last_rsi_arr[-1] > 50 else -1)
    else:
        scores.append(0)

    # 2. MACD histogram — short-term momentum
    _, _, hist = ind.macd(C, 12, 26, 9)
    last_hist = hist[~np.isnan(hist)]
    if len(last_hist):
        scores.append(+1 if last_hist[-1] > 0 else -1)
    else:
        scores.append(0)

    # 3. Price vs Bollinger middle band — trend position
    _, mid, _ = ind.bollinger_bands(C, 20, 2)
    last_mid = mid[~np.isnan(mid)]
    if len(last_mid):
        scores.append(+1 if C[-1] > last_mid[-1] else -1)
    else:
        scores.append(0)

    # 4. StochRSI — overbought/oversold (neutral zone = 0, not penalised)
    k, _ = ind.stoch_rsi(C, 14, 14, 3, 3)
    last_k = k[~np.isnan(k)]
    if len(last_k):
        if last_k[-1] < 0.2:
            scores.append(+1)   # Oversold → bullish
        elif last_k[-1] > 0.8:
            scores.append(-1)   # Overbought → bearish
        else:
            scores.append(0)    # Neutral — no opinion
    else:
        scores.append(0)

    # 5. ADX trend direction
    adx_vals, pdi, mdi = ind.adx(H, L, C, 14)
    last_adx = adx_vals[~np.isnan(adx_vals)]
    if len(last_adx) and last_adx[-1] > 20:
        last_pdi = pdi[~np.isnan(adx_vals)]
        last_mdi = mdi[~np.isnan(adx_vals)]
        if len(last_pdi):
            scores.append(+1 if last_pdi[-1] > last_mdi[-1] else -1)
        else:
            scores.append(0)
    else:
        scores.append(0)

    # 6. EMA9 vs EMA21 crossover (replaces ATR — more directional signal)
    ema9_arr  = ind.ema(C, 9)
    ema21_arr = ind.ema(C, 21)
    v9  = ema9_arr[~np.isnan(ema9_arr)]
    v21 = ema21_arr[~np.isnan(ema21_arr)]
    if len(v9) and len(v21):
        scores.append(+1 if v9[-1] > v21[-1] else -1)
    else:
        scores.append(0)

    # 7. Recent price momentum (last 3 bars making HH or LL)
    if len(H) >= 3 and all(H[-i] > H[-i-1] for i in range(1, 3)):
        scores.append(+1)
    elif len(L) >= 3 and all(L[-i] < L[-i-1] for i in range(1, 3)):
        scores.append(-1)
    else:
        scores.append(0)

    # Count only non-neutral votes
    non_neutral = [s for s in scores if s != 0]
    raw_score   = sum(non_neutral)

    logger.debug(f"Module3 votes: {scores} → raw={raw_score} needed=±{min_votes}")

    if   raw_score >= min_votes:  return +1
    elif raw_score <= -min_votes: return -1
    return 0


# ─── Main Signal Aggregator ───────────────────────────────────────────────────

class SignalEngine:

    def __init__(self, min_modules: int = 2, min_votes: int = 4):
        self.min_modules = min_modules   # 2/3 required — accuracy gate
        self.min_votes   = min_votes     # 4/7 required — quality gate

    def evaluate(self,
                 ltf_bars: List[Candle],
                 htf_bias: str,
                 smc_ctx:  SMCContext,
                 in_zone:  bool) -> SignalResult:

        # Hard gate: no bias or not in zone → no trade
        if htf_bias == "NEUTRAL" or not in_zone:
            return SignalResult("NONE", 0, 0, 0, 0, 0,
                                f"htf_bias={htf_bias}, in_zone={in_zone}")

        m1 = module1_mtfa_rsi(ltf_bars, htf_bias)
        m2 = module2_candlestick(ltf_bars)
        m3 = module3_vote(ltf_bars, self.min_votes)

        expected   = +1 if htf_bias == "LONG" else -1
        confirming = sum(1 for m in [m1, m2, m3] if m == expected)

        logger.debug(f"Signal modules: m1={m1} m2={m2} m3={m3} "
                     f"confirming={confirming}/{self.min_modules} bias={htf_bias} "
                     f"trend_strength={smc_ctx.trend_strength:.2f}")

        if confirming < self.min_modules:
            reason = (f"only {confirming}/{self.min_modules} modules confirmed "
                      f"(m1={m1}, m2={m2}, m3={m3})")
            return SignalResult("NONE", confirming, m1, m2, m3, confirming, reason)

        direction = "LONG" if htf_bias == "LONG" else "SHORT"
        reason    = (f"✓ {confirming}/3 modules | bias={htf_bias} | "
                     f"m1={m1} m2={m2} m3={m3} | "
                     f"trend_strength={smc_ctx.trend_strength:.2f}")
        logger.info(f"Signal: {direction} | {reason}")

        return SignalResult(direction, confirming, m1, m2, m3, confirming, reason)

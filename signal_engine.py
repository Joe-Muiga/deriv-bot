"""
signal_engine.py – Phase B of SIFM: Lower-Timeframe Entry Scan.

v5 → v6 changes:
  1. Module 1: unchanged (slope OR RSI divergence, OR-gate kept).
     RSI divergence lookback reduced 20 → 10 so it fires on the 30–50-bar
     windows typical of 1-min synthetic charts.

  2. Module 2: unchanged (volume guard skipped for synthetics, pin bar /
     shooting star patterns included).

  3. Module 3 – weighted votes:
     RSI(14), MACD histogram, and Bollinger mid-band are the most reliable
     momentum indicators on synthetic instruments.  Each now contributes
     2 votes instead of 1 (total vote pool = 10; neutral votes = 0 as before).
     StochRSI, ADX, ATR-direction, and Price-Structure each contribute 1 vote.
     min_votes default kept at 3 — but because RSI+MACD alone yield 4 votes
     when they agree, any two reliable indicators aligning is now sufficient.

     Vote labels updated to show weight: e.g. "RSI×2=+2".

  4. Debug logging added at each module level (carried over from v5).
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
    direction: str    # "LONG" | "SHORT" | "NONE"
    strength:  float  # 0–3 (confirming modules)
    m1_signal: int
    m2_signal: int
    m3_signal: int
    m3_score:  int
    reason:    str


# ─── Module 1 – MTFA + RSI Divergence ────────────────────────────────────────

def module1_mtfa_rsi(ltf_bars: List[Candle], htf_bias: str) -> int:
    """
    Fires on EITHER EMA slope alignment OR RSI divergence (OR-gate).
    Divergence lookback reduced 20 → 10 to fire on 30–50-bar windows
    typical of 1-min synthetic charts.
    """
    if len(ltf_bars) < 25:
        return 0
    closes = np.array([b.close for b in ltf_bars])

    ema20 = ind.ema(closes, 20)
    rsi14 = ind.rsi(closes, 14)
    div   = ind.find_rsi_divergence(closes, rsi14, lookback=10)  # was 20

    valid_ema = ema20[~np.isnan(ema20)]
    if len(valid_ema) < 2:
        return 0
    slope = valid_ema[-1] - valid_ema[-2]

    # Primary path: EMA slope aligned with HTF bias
    slope_ok_long  = htf_bias == "LONG"  and slope > 0
    slope_ok_short = htf_bias == "SHORT" and slope < 0

    # Secondary path: RSI divergence
    div_ok_long  = htf_bias == "LONG"  and div == +1
    div_ok_short = htf_bias == "SHORT" and div == -1

    if slope_ok_long  or div_ok_long:
        logger.debug(
            f"M1 +1 | slope={slope:+.6f} slope_ok={slope_ok_long} div={div}")
        return +1
    if slope_ok_short or div_ok_short:
        logger.debug(
            f"M1 -1 | slope={slope:+.6f} slope_ok={slope_ok_short} div={div}")
        return -1

    logger.debug(f"M1  0 | slope={slope:+.6f} div={div} bias={htf_bias}")
    return 0


# ─── Module 2 – Candlestick Confluence ───────────────────────────────────────

def module2_candlestick(ltf_bars: List[Candle]) -> int:
    """
    Volume guard skipped for synthetic instruments (avg_vol == 0).
    Multiplier 1.2× for real instruments.
    Patterns: engulfing, morning/evening star, three-line strike, pin bar,
    shooting star.
    """
    if len(ltf_bars) < 4:
        return 0

    last, prev, prev2 = ltf_bars[-1], ltf_bars[-2], ltf_bars[-3]
    volumes = [b.volume for b in ltf_bars[-21:]]
    avg_vol = float(np.mean(volumes[:-1])) if len(volumes) > 1 else 0.0

    if avg_vol == 0:
        vol_ok = True
    else:
        vol_ok = last.volume > 1.2 * avg_vol

    def is_bull(b):       return b.close > b.open
    def is_bear(b):       return b.close < b.open
    def body(b):          return abs(b.close - b.open)
    def upper_wick(b):    return b.high - max(b.open, b.close)
    def lower_wick(b):    return min(b.open, b.close) - b.low
    def candle_range(b):  return b.high - b.low if b.high != b.low else 1e-10

    # ── Bullish patterns ──────────────────────────────────────────────────────

    if (is_bear(prev) and is_bull(last) and
            last.open <= prev.close and last.close >= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("M2 +1 | bullish engulfing")
        return +1

    if (is_bear(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bull(last) and last.close > (prev2.open + prev2.close) / 2 and
            vol_ok):
        logger.debug("M2 +1 | morning star")
        return +1

    if len(ltf_bars) >= 5:
        c0, c1, c2, c3 = (ltf_bars[-5], ltf_bars[-4],
                          ltf_bars[-3], ltf_bars[-2])
        if (all(is_bear(x) for x in [c0, c1, c2, c3]) and
                is_bull(last) and last.close >= c0.open and vol_ok):
            logger.debug("M2 +1 | three-line strike bullish")
            return +1

    if (lower_wick(last) > 2.0 * body(last) and
            lower_wick(last) > upper_wick(last) * 2.0 and
            body(last) / candle_range(last) < 0.35 and vol_ok):
        logger.debug("M2 +1 | bullish pin bar / hammer")
        return +1

    # ── Bearish patterns ──────────────────────────────────────────────────────

    if (is_bull(prev) and is_bear(last) and
            last.open >= prev.close and last.close <= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("M2 -1 | bearish engulfing")
        return -1

    if (is_bull(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bear(last) and last.close < (prev2.open + prev2.close) / 2 and
            vol_ok):
        logger.debug("M2 -1 | evening star")
        return -1

    if len(ltf_bars) >= 5:
        c0, c1, c2, c3 = (ltf_bars[-5], ltf_bars[-4],
                          ltf_bars[-3], ltf_bars[-2])
        if (all(is_bull(x) for x in [c0, c1, c2, c3]) and
                is_bear(last) and last.close <= c0.open and vol_ok):
            logger.debug("M2 -1 | three-line strike bearish")
            return -1

    if (upper_wick(last) > 2.0 * body(last) and
            upper_wick(last) > lower_wick(last) * 2.0 and
            body(last) / candle_range(last) < 0.35 and vol_ok):
        logger.debug("M2 -1 | bearish shooting star")
        return -1

    logger.debug("M2  0 | no pattern")
    return 0


# ─── Module 3 – Weighted 10-Vote Quantitative Panel ──────────────────────────

def module3_vote(ltf_bars: List[Candle], min_votes: int = 3) -> int:
    """
    Weighted vote panel (total pool = 10 votes):

      RSI(14)      × 2   – reliable momentum on all timeframes
      MACD hist    × 2   – momentum direction + histogram sign
      Bollinger    × 2   – price vs mean-reversion anchor
      StochRSI     × 1   – oversold/overbought only (neutral zone = 0)
      ADX trend    × 1   – trend strength (non-trending = 0)
      ATR direction× 1   – expanding vs contracting volatility
      Price struct × 1   – HH/HL or LH/LL in last 3 bars

    min_votes = 3 (default unchanged).
    Because RSI + MACD together contribute 4 bull votes when they agree,
    any two reliable indicators pointing the same way crosses the threshold.

    Neutral votes (StochRSI in 0.2–0.8, ADX < 25, flat ATR, ranging price)
    contribute 0 and do NOT dilute the directional count.
    """
    if len(ltf_bars) < 30:
        return 0

    H = np.array([b.high  for b in ltf_bars])
    L = np.array([b.low   for b in ltf_bars])
    C = np.array([b.close for b in ltf_bars])

    bull_votes = 0
    bear_votes = 0
    labels     = []

    # ── 1. RSI(14) × 2 ────────────────────────────────────────────────────────
    rsi14    = ind.rsi(C, 14)
    last_rsi = rsi14[~np.isnan(rsi14)]
    if len(last_rsi):
        v = +1 if last_rsi[-1] > 50 else -1
    else:
        v = 0
    bull_votes += max(0,  v * 2)
    bear_votes += max(0, -v * 2)
    labels.append(f"RSI×2={v * 2:+d}")

    # ── 2. MACD histogram × 2 ─────────────────────────────────────────────────
    _, _, hist = ind.macd(C, 12, 26, 9)
    last_hist  = hist[~np.isnan(hist)]
    if len(last_hist):
        v = +1 if last_hist[-1] > 0 else -1
    else:
        v = 0
    bull_votes += max(0,  v * 2)
    bear_votes += max(0, -v * 2)
    labels.append(f"MACD×2={v * 2:+d}")

    # ── 3. Price vs Bollinger middle × 2 ──────────────────────────────────────
    _, mid, _ = ind.bollinger_bands(C, 20, 2)
    last_mid  = mid[~np.isnan(mid)]
    if len(last_mid):
        v = +1 if C[-1] > last_mid[-1] else -1
    else:
        v = 0
    bull_votes += max(0,  v * 2)
    bear_votes += max(0, -v * 2)
    labels.append(f"BB×2={v * 2:+d}")

    # ── 4. StochRSI extreme zones × 1 ────────────────────────────────────────
    k, _ = ind.stoch_rsi(C, 14, 14, 3, 3)
    last_k = k[~np.isnan(k)]
    if len(last_k):
        val = last_k[-1]
        if   val < 0.2: v = +1
        elif val > 0.8: v = -1
        else:           v = 0
    else:
        v = 0
    bull_votes += max(0,  v)
    bear_votes += max(0, -v)
    labels.append(f"StochRSI={v:+d}")

    # ── 5. ADX trend direction × 1 ────────────────────────────────────────────
    adx_vals, pdi, mdi = ind.adx(H, L, C, 14)
    last_adx = adx_vals[~np.isnan(adx_vals)]
    if len(last_adx) and last_adx[-1] > 25:
        # Align pdi/mdi to last_adx length
        offset   = len(adx_vals) - len(last_adx)
        last_pdi = pdi[offset:]
        last_mdi = mdi[offset:]
        v = +1 if last_pdi[-1] > last_mdi[-1] else -1
    else:
        v = 0
    bull_votes += max(0,  v)
    bear_votes += max(0, -v)
    labels.append(f"ADX={v:+d}")

    # ── 6. ATR direction × 1 ─────────────────────────────────────────────────
    atr14     = ind.atr(H, L, C, 14)
    valid_atr = atr14[~np.isnan(atr14)]
    if len(valid_atr) >= 3:
        v = +1 if valid_atr[-1] > valid_atr[-2] else -1
    else:
        v = 0
    bull_votes += max(0,  v)
    bear_votes += max(0, -v)
    labels.append(f"ATR={v:+d}")

    # ── 7. Price-action structure × 1 ────────────────────────────────────────
    if len(H) >= 3 and all(H[-i] > H[-i - 1] for i in range(1, 3)):
        v = +1
    elif len(L) >= 3 and all(L[-i] < L[-i - 1] for i in range(1, 3)):
        v = -1
    else:
        v = 0
    bull_votes += max(0,  v)
    bear_votes += max(0, -v)
    labels.append(f"Struct={v:+d}")

    logger.debug(
        f"M3 votes: {' '.join(labels)} | bull={bull_votes} bear={bear_votes} "
        f"threshold={min_votes}")

    if   bull_votes >= min_votes: return +1
    elif bear_votes >= min_votes: return -1
    return 0


# ─── Signal aggregator ────────────────────────────────────────────────────────

class SignalEngine:

    def __init__(self, min_modules: int = 2, min_votes: int = 3):
        self.min_modules = min_modules
        self.min_votes   = min_votes

    def evaluate(self, ltf_bars: List[Candle], htf_bias: str,
                 smc_ctx: SMCContext, in_zone: bool) -> SignalResult:

        if htf_bias == "NEUTRAL" or not in_zone:
            return SignalResult("NONE", 0, 0, 0, 0, 0,
                                f"htf_bias={htf_bias}, in_zone={in_zone}")

        m1 = module1_mtfa_rsi(ltf_bars, htf_bias)
        m2 = module2_candlestick(ltf_bars)
        m3 = module3_vote(ltf_bars, self.min_votes)

        expected   = +1 if htf_bias == "LONG" else -1
        confirming = sum(1 for m in [m1, m2, m3] if m == expected)

        logger.debug(
            f"SignalEngine | bias={htf_bias} expected={expected:+d} "
            f"m1={m1} m2={m2} m3={m3} confirming={confirming}/{self.min_modules}")

        if confirming < self.min_modules:
            reason = (f"only {confirming}/{self.min_modules} modules confirmed "
                      f"(m1={m1}, m2={m2}, m3={m3})")
            return SignalResult("NONE", confirming, m1, m2, m3,
                                confirming, reason)

        direction = "LONG" if htf_bias == "LONG" else "SHORT"
        reason    = (f"✓ {confirming}/3 modules | bias={htf_bias} | "
                     f"m1={m1} m2={m2} m3={m3}")
        logger.info(f"Signal: {direction} | {reason}")

        return SignalResult(direction, confirming, m1, m2, m3,
                            confirming, reason)

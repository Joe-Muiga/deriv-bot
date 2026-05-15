"""
signal_engine.py – Phase B of SIFM: Lower-Timeframe Entry Scan.

Fixes applied (v5):
  1. Module 1 AND-trap removed: slope alignment OR RSI divergence is sufficient
     (requiring both simultaneously made M1 fire ~5% of the time).
  2. Module 2 volume guard fixed: synthetics have volume=0; guard is now skipped
     when avg_vol == 0 so candlestick patterns are never silently blocked.
     Volume multiplier also reduced from 1.5× to 1.2× for real instruments.
     Two additional entry patterns added (pin bar / shooting star).
  3. Module 3 min_votes default lowered from 4 → 3: a clear 3-of-7 directional
     majority is sufficient; 4 required near-perfect consensus that rarely
     triggered on choppy or ranging synthetic markets.
  4. Debug logging added at each module level so you can see per-bar decisions
     in the log without waiting for a full signal.
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
    strength:  float  # 0–3  (confirming modules)
    m1_signal: int
    m2_signal: int
    m3_signal: int
    m3_score:  int
    reason:    str


# ─── Module 1 – MTFA + RSI Divergence ────────────────────────────────────────

def module1_mtfa_rsi(ltf_bars: List[Candle], htf_bias: str) -> int:
    """
    Fix: previously required BOTH EMA slope AND RSI divergence simultaneously.
    Now fires on EITHER condition alone — slope alignment is the primary signal,
    divergence is an independent confirmation path.
    """
    if len(ltf_bars) < 25:
        return 0
    closes = np.array([b.close for b in ltf_bars])

    ema20 = ind.ema(closes, 20)
    rsi14 = ind.rsi(closes, 14)
    div   = ind.find_rsi_divergence(closes, rsi14, lookback=20)

    valid_ema = ema20[~np.isnan(ema20)]
    if len(valid_ema) < 2:
        return 0
    slope = valid_ema[-1] - valid_ema[-2]

    # Primary path: EMA slope aligned with HTF bias
    slope_ok_long  = htf_bias == "LONG"  and slope > 0
    slope_ok_short = htf_bias == "SHORT" and slope < 0

    # Secondary path: RSI divergence confirms HTF bias
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
    Fix: volume guard was permanently blocking signals on synthetic instruments
    (volume is 0 on algorithmically-generated indices → avg_vol=0 → vol_ok
    always False).  Guard is now skipped when avg_vol==0.  Multiplier reduced
    from 1.5× to 1.2× for real instruments.  Pin bar / shooting-star added.
    """
    if len(ltf_bars) < 4:
        return 0

    last, prev, prev2 = ltf_bars[-1], ltf_bars[-2], ltf_bars[-3]
    volumes = [b.volume for b in ltf_bars[-21:]]
    avg_vol = float(np.mean(volumes[:-1])) if len(volumes) > 1 else 0.0

    # Skip volume check entirely for synthetic instruments (avg_vol == 0)
    if avg_vol == 0:
        vol_ok = True
    else:
        vol_ok = last.volume > 1.2 * avg_vol

    def is_bull(b): return b.close > b.open
    def is_bear(b): return b.close < b.open
    def body(b):    return abs(b.close - b.open)
    def upper_wick(b): return b.high - max(b.open, b.close)
    def lower_wick(b): return min(b.open, b.close) - b.low
    def candle_range(b): return b.high - b.low if b.high != b.low else 1e-10

    # ── Bullish patterns ──────────────────────────────────────────────────────

    # Bullish engulfing
    if (is_bear(prev) and is_bull(last) and
            last.open <= prev.close and last.close >= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("M2 +1 | bullish engulfing")
        return +1

    # Morning star
    if (is_bear(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bull(last) and last.close > (prev2.open + prev2.close) / 2 and
            vol_ok):
        logger.debug("M2 +1 | morning star")
        return +1

    # Three-line strike (bullish)
    if len(ltf_bars) >= 5:
        c0, c1, c2, c3 = ltf_bars[-5], ltf_bars[-4], ltf_bars[-3], ltf_bars[-2]
        if (all(is_bear(x) for x in [c0, c1, c2, c3]) and
                is_bull(last) and last.close >= c0.open and vol_ok):
            logger.debug("M2 +1 | three-line strike bullish")
            return +1

    # Bullish pin bar (hammer): long lower wick, small body near top
    if (lower_wick(last) > 2.0 * body(last) and
            lower_wick(last) > upper_wick(last) * 2.0 and
            body(last) / candle_range(last) < 0.35 and vol_ok):
        logger.debug("M2 +1 | bullish pin bar / hammer")
        return +1

    # ── Bearish patterns ──────────────────────────────────────────────────────

    # Bearish engulfing
    if (is_bull(prev) and is_bear(last) and
            last.open >= prev.close and last.close <= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("M2 -1 | bearish engulfing")
        return -1

    # Evening star
    if (is_bull(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bear(last) and last.close < (prev2.open + prev2.close) / 2 and
            vol_ok):
        logger.debug("M2 -1 | evening star")
        return -1

    # Three-line strike (bearish)
    if len(ltf_bars) >= 5:
        c0, c1, c2, c3 = ltf_bars[-5], ltf_bars[-4], ltf_bars[-3], ltf_bars[-2]
        if (all(is_bull(x) for x in [c0, c1, c2, c3]) and
                is_bear(last) and last.close <= c0.open and vol_ok):
            logger.debug("M2 -1 | three-line strike bearish")
            return -1

    # Bearish shooting star: long upper wick, small body near bottom
    if (upper_wick(last) > 2.0 * body(last) and
            upper_wick(last) > lower_wick(last) * 2.0 and
            body(last) / candle_range(last) < 0.35 and vol_ok):
        logger.debug("M2 -1 | bearish shooting star")
        return -1

    logger.debug("M2  0 | no pattern")
    return 0


# ─── Module 3 – 7-Indicator Quantitative Vote ─────────────────────────────────

def module3_vote(ltf_bars: List[Candle], min_votes: int = 3) -> int:
    """
    Each of 7 indicators votes +1 (bullish), -1 (bearish), or 0 (neutral).
    Signal fires when one direction accumulates >= min_votes independent votes.
    Neutral votes are ignored – they do not dilute a directional majority.

    Fix: default min_votes lowered from 4 → 3.  With votes 4, 5, 7 often
    returning 0 (neutral) in ranging/synthetic markets, 4 required near-perfect
    consensus that was rarely achieved.  3 votes still demands a clear majority
    from truly directional indicators.
    """
    if len(ltf_bars) < 30:
        return 0

    H = np.array([b.high  for b in ltf_bars])
    L = np.array([b.low   for b in ltf_bars])
    C = np.array([b.close for b in ltf_bars])

    votes = []
    labels = []

    # 1. RSI(14) side of 50
    rsi14    = ind.rsi(C, 14)
    last_rsi = rsi14[~np.isnan(rsi14)]
    v = +1 if (len(last_rsi) and last_rsi[-1] > 50) else -1
    votes.append(v); labels.append(f"RSI={v}")

    # 2. MACD histogram sign
    _, _, hist = ind.macd(C, 12, 26, 9)
    last_hist  = hist[~np.isnan(hist)]
    v = +1 if (len(last_hist) and last_hist[-1] > 0) else -1
    votes.append(v); labels.append(f"MACD={v}")

    # 3. Price vs Bollinger middle band
    _, mid, _ = ind.bollinger_bands(C, 20, 2)
    last_mid  = mid[~np.isnan(mid)]
    v = +1 if (len(last_mid) and C[-1] > last_mid[-1]) else -1
    votes.append(v); labels.append(f"BB={v}")

    # 4. StochRSI extreme zones (0 = neutral / trending)
    k, _ = ind.stoch_rsi(C, 14, 14, 3, 3)
    last_k = k[~np.isnan(k)]
    if len(last_k):
        if   last_k[-1] < 0.2: v = +1
        elif last_k[-1] > 0.8: v = -1
        else:                   v = 0   # neutral – does not count
    else:
        v = 0
    votes.append(v); labels.append(f"StochRSI={v}")

    # 5. ADX trend direction (0 when not trending)
    adx_vals, pdi, mdi = ind.adx(H, L, C, 14)
    last_adx = adx_vals[~np.isnan(adx_vals)]
    if len(last_adx) and last_adx[-1] > 25:
        last_pdi = pdi[len(pdi) - len(last_adx):]
        last_mdi = mdi[len(mdi) - len(last_adx):]
        v = +1 if last_pdi[-1] > last_mdi[-1] else -1
    else:
        v = 0   # not trending – does not count
    votes.append(v); labels.append(f"ADX={v}")

    # 6. ATR direction (rising = expanding momentum)
    atr14     = ind.atr(H, L, C, 14)
    valid_atr = atr14[~np.isnan(atr14)]
    if len(valid_atr) >= 3:
        v = +1 if valid_atr[-1] > valid_atr[-2] else -1
    else:
        v = 0
    votes.append(v); labels.append(f"ATR={v}")

    # 7. Recent price-action structure
    if len(H) >= 3 and all(H[-i] > H[-i-1] for i in range(1, 3)):
        v = +1
    elif len(L) >= 3 and all(L[-i] < L[-i-1] for i in range(1, 3)):
        v = -1
    else:
        v = 0
    votes.append(v); labels.append(f"Struct={v}")

    bull_votes = sum(1 for v in votes if v > 0)
    bear_votes = sum(1 for v in votes if v < 0)

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
            return SignalResult("NONE", confirming, m1, m2, m3, confirming, reason)

        direction = "LONG" if htf_bias == "LONG" else "SHORT"
        reason    = (f"✓ {confirming}/3 modules | bias={htf_bias} | "
                     f"m1={m1} m2={m2} m3={m3}")
        logger.info(f"Signal: {direction} | {reason}")

        return SignalResult(direction, confirming, m1, m2, m3, confirming, reason)

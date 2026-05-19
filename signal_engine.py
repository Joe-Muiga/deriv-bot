"""
signal_engine.py – Phase B of SIFM: Lower-Timeframe Entry Scan.

v9 → v10 changes (FIX 2):

  VALIDATION RESTORED TO ORIGINAL DIRECTION:
    _validate_recent_price_action() now validates against the ACTUAL signal
    direction ("LONG" or "SHORT") — never against an inverted direction.
    Any previous workaround that passed an inverted direction string to
    this function has been removed.  The function is unchanged in logic;
    only the caller (evaluate()) is confirmed to pass direction directly.

  NO OTHER CHANGES:
    All v9 logic preserved: module functions, confidence counter,
    vote panel, module3_confidence(), SignalResult dataclass.
"""

import numpy as np
import logging
from dataclasses import dataclass
from typing import List
from candlestick_builder import Candle
import indicators as ind
from smc_analyzer import SMCContext

logger = logging.getLogger(__name__)

_VALIDATION_NET_TOLERANCE = 0.0010
_VALIDATION_LOOKBACK      = 5
_VALIDATION_MIN_AGREEMENT = 2


@dataclass
class SignalResult:
    direction:  str    # "LONG" | "SHORT" | "NONE"
    strength:   float  # 0–3 (confirming modules)
    m1_signal:  int
    m2_signal:  int
    m3_signal:  int
    m3_score:   int
    reason:     str
    confidence: int = 0   # 0–7: how many of 7 M3 indicators agree with direction


# ─── Signal self-validation ───────────────────────────────────────────────────

def _validate_recent_price_action(ltf_bars: List[Candle],
                                  direction: str,
                                  lookback: int = _VALIDATION_LOOKBACK) -> bool:
    """
    Validates that recent price action is consistent with *direction*.
    direction must be "LONG" or "SHORT" — the actual signal direction,
    never an inverted version.
    """
    if len(ltf_bars) < lookback + 1:
        return True

    recent = ltf_bars[-(lookback + 1): -1]
    if len(recent) < lookback:
        return True

    start_price = float(recent[0].close)
    end_price   = float(recent[-1].close)
    ref         = start_price if start_price != 0 else 1.0
    net_pct     = (end_price - start_price) / ref

    if direction == "LONG"  and net_pct < -_VALIDATION_NET_TOLERANCE:
        logger.debug(
            f"Signal validation FAILED (LONG): net_pct={net_pct:.5f} < "
            f"-{_VALIDATION_NET_TOLERANCE} — price falling over last {lookback} bars")
        return False

    if direction == "SHORT" and net_pct > +_VALIDATION_NET_TOLERANCE:
        logger.debug(
            f"Signal validation FAILED (SHORT): net_pct={net_pct:.5f} > "
            f"+{_VALIDATION_NET_TOLERANCE} — price rising over last {lookback} bars")
        return False

    min_agree = _VALIDATION_MIN_AGREEMENT
    if direction == "LONG":
        agreeing = sum(1 for b in recent if b.close >= b.open)
    else:
        agreeing = sum(1 for b in recent if b.close <= b.open)

    if agreeing < min_agree:
        logger.debug(
            f"Signal validation FAILED ({direction}): only {agreeing}/{lookback} "
            f"bars agree (need {min_agree}) — candle bodies contradict signal")
        return False

    logger.debug(
        f"Signal validation PASSED ({direction}): net_pct={net_pct:.5f}, "
        f"agreeing={agreeing}/{lookback}")
    return True


# ─── Module 1 – MTFA + RSI Divergence ────────────────────────────────────────

def module1_mtfa_rsi(ltf_bars: List[Candle], htf_bias: str) -> int:
    if len(ltf_bars) < 25:
        return 0
    closes = np.array([b.close for b in ltf_bars])

    ema20 = ind.ema(closes, 20)
    rsi14 = ind.rsi(closes, 14)
    div   = ind.find_rsi_divergence(closes, rsi14, lookback=10)

    valid_ema = ema20[~np.isnan(ema20)]
    if len(valid_ema) < 2:
        return 0
    slope = valid_ema[-1] - valid_ema[-2]

    slope_ok_long  = htf_bias == "LONG"  and slope > 0
    slope_ok_short = htf_bias == "SHORT" and slope < 0
    div_ok_long    = htf_bias == "LONG"  and div == +1
    div_ok_short   = htf_bias == "SHORT" and div == -1

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
    Weighted vote panel (total pool = 10 votes, 7 independent indicators).

    Returns +1 (bull), -1 (bear), or 0.
    Does NOT return the per-indicator breakdown — use module3_confidence() for that.
    """
    if len(ltf_bars) < 30:
        return 0

    H = np.array([b.high  for b in ltf_bars])
    L = np.array([b.low   for b in ltf_bars])
    C = np.array([b.close for b in ltf_bars])

    bull_votes = 0
    bear_votes = 0
    labels     = []

    rsi14    = ind.rsi(C, 14)
    last_rsi = rsi14[~np.isnan(rsi14)]
    if len(last_rsi):
        v = +1 if last_rsi[-1] > 50 else -1
    else:
        v = 0
    bull_votes += max(0,  v * 2)
    bear_votes += max(0, -v * 2)
    labels.append(f"RSI×2={v * 2:+d}")

    _, _, hist = ind.macd(C, 12, 26, 9)
    last_hist  = hist[~np.isnan(hist)]
    if len(last_hist):
        v = +1 if last_hist[-1] > 0 else -1
    else:
        v = 0
    bull_votes += max(0,  v * 2)
    bear_votes += max(0, -v * 2)
    labels.append(f"MACD×2={v * 2:+d}")

    _, mid, _ = ind.bollinger_bands(C, 20, 2)
    last_mid  = mid[~np.isnan(mid)]
    if len(last_mid):
        v = +1 if C[-1] > last_mid[-1] else -1
    else:
        v = 0
    bull_votes += max(0,  v * 2)
    bear_votes += max(0, -v * 2)
    labels.append(f"BB×2={v * 2:+d}")

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

    adx_vals, pdi, mdi = ind.adx(H, L, C, 14)
    last_adx = adx_vals[~np.isnan(adx_vals)]
    if len(last_adx) and last_adx[-1] > 25:
        offset   = len(adx_vals) - len(last_adx)
        last_pdi = pdi[offset:]
        last_mdi = mdi[offset:]
        v = +1 if last_pdi[-1] > last_mdi[-1] else -1
    else:
        v = 0
    bull_votes += max(0,  v)
    bear_votes += max(0, -v)
    labels.append(f"ADX={v:+d}")

    atr14     = ind.atr(H, L, C, 14)
    valid_atr = atr14[~np.isnan(atr14)]
    if len(valid_atr) >= 3 and len(C) >= 3:
        atr_expanding = valid_atr[-1] > valid_atr[-2]
        price_rising  = float(C[-1]) > float(C[-2])
        if atr_expanding:
            v = +1 if price_rising else -1
        else:
            v = 0
    else:
        v = 0
    bull_votes += max(0,  v)
    bear_votes += max(0, -v)
    labels.append(f"ATR_dir={v:+d}")

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


def module3_confidence(ltf_bars: List[Candle], direction: str) -> int:
    """
    Count how many of the 7 individual M3 indicators agree with `direction`.
    Each indicator contributes 1 if it agrees, 0 otherwise.
    Returns an int in [0, 7].

    This is called AFTER the signal direction is determined, so `direction`
    is always "LONG" or "SHORT" — never "NONE".
    """
    if len(ltf_bars) < 30 or direction not in ("LONG", "SHORT"):
        return 0

    expected = +1 if direction == "LONG" else -1
    H = np.array([b.high  for b in ltf_bars])
    L = np.array([b.low   for b in ltf_bars])
    C = np.array([b.close for b in ltf_bars])
    count = 0

    # 1. RSI
    rsi14    = ind.rsi(C, 14)
    last_rsi = rsi14[~np.isnan(rsi14)]
    if len(last_rsi):
        v = +1 if last_rsi[-1] > 50 else -1
        if v == expected:
            count += 1

    # 2. MACD histogram
    _, _, hist = ind.macd(C, 12, 26, 9)
    last_hist  = hist[~np.isnan(hist)]
    if len(last_hist):
        v = +1 if last_hist[-1] > 0 else -1
        if v == expected:
            count += 1

    # 3. Bollinger middle
    _, mid, _ = ind.bollinger_bands(C, 20, 2)
    last_mid  = mid[~np.isnan(mid)]
    if len(last_mid):
        v = +1 if C[-1] > last_mid[-1] else -1
        if v == expected:
            count += 1

    # 4. StochRSI
    k, _ = ind.stoch_rsi(C, 14, 14, 3, 3)
    last_k = k[~np.isnan(k)]
    if len(last_k):
        val = last_k[-1]
        if   val < 0.2: v = +1
        elif val > 0.8: v = -1
        else:           v = 0
        if v == expected:
            count += 1

    # 5. ADX direction
    adx_vals, pdi, mdi = ind.adx(H, L, C, 14)
    last_adx = adx_vals[~np.isnan(adx_vals)]
    if len(last_adx) and last_adx[-1] > 25:
        offset   = len(adx_vals) - len(last_adx)
        v = +1 if pdi[offset:][-1] > mdi[offset:][-1] else -1
        if v == expected:
            count += 1

    # 6. ATR + price direction
    atr14     = ind.atr(H, L, C, 14)
    valid_atr = atr14[~np.isnan(atr14)]
    if len(valid_atr) >= 3 and len(C) >= 3:
        atr_expanding = valid_atr[-1] > valid_atr[-2]
        price_rising  = float(C[-1]) > float(C[-2])
        if atr_expanding:
            v = +1 if price_rising else -1
            if v == expected:
                count += 1

    # 7. Price structure
    if len(H) >= 3 and all(H[-i] > H[-i - 1] for i in range(1, 3)):
        v = +1
    elif len(L) >= 3 and all(L[-i] < L[-i - 1] for i in range(1, 3)):
        v = -1
    else:
        v = 0
    if v == expected:
        count += 1

    logger.debug(f"M3 confidence={count}/7 for {direction}")
    return count


# ─── Signal aggregator ────────────────────────────────────────────────────────

class SignalEngine:

    def __init__(self, min_modules: int = 2, min_votes: int = 3):
        self.min_modules = min_modules
        self.min_votes   = min_votes

    def evaluate(self, ltf_bars: List[Candle], htf_bias: str,
                 smc_ctx: SMCContext, in_zone: bool) -> SignalResult:
        """
        Evaluate all three modules and return a directional signal.

        FIX 2: Direction is derived directly from htf_bias — NEVER inverted.
          htf_bias == "LONG"  → direction = "LONG"
          htf_bias == "SHORT" → direction = "SHORT"

        Validation checks the ACTUAL direction (not an inverted version).
        Confidence is computed for the ACTUAL direction.
        """
        if htf_bias == "NEUTRAL" or not in_zone:
            return SignalResult("NONE", 0, 0, 0, 0, 0,
                                f"htf_bias={htf_bias}, in_zone={in_zone}",
                                confidence=0)

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
                                confirming, reason, confidence=0)

        # FIX 2: direction follows htf_bias directly — no inversion
        direction = "LONG" if htf_bias == "LONG" else "SHORT"

        # Validate against ACTUAL direction (never inverted)
        if not _validate_recent_price_action(ltf_bars, direction):
            reason = (f"signal validation FAILED: recent bars contradict "
                      f"{direction} | m1={m1} m2={m2} m3={m3}")
            logger.info(f"Signal REJECTED by validation: {direction} | {reason}")
            return SignalResult("NONE", confirming, m1, m2, m3,
                                confirming, reason, confidence=0)

        # Compute confidence for the ACTUAL direction
        confidence = module3_confidence(ltf_bars, direction)

        reason = (f"✓ {confirming}/3 modules | bias={htf_bias} | "
                  f"m1={m1} m2={m2} m3={m3} | "
                  f"confidence={confidence}/7 | validation=PASS")
        logger.info(f"Signal: {direction} | {reason}")

        return SignalResult(direction, confirming, m1, m2, m3,
                            confirming, reason, confidence=confidence)

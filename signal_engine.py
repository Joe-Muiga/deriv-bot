"""
signal_engine.py – Phase B of SIFM: Lower-Timeframe Entry Scan.

Key fix in this revision
------------------------
Module 1 previously used OR logic:
    if ema_aligned OR rsi_ok OR divergence → fire
This fired on ANY single condition and was the primary source of false
signals.  It is now AND logic:
    EMA alignment is MANDATORY.
    RSI confirmation OR divergence serves as the secondary gate.
So the full condition is:  ema_aligned AND (rsi_ok OR divergence)

Module 3 now returns (signal, raw_vote_count) so BotEngine can incorporate
vote density into the composite probability score.

SignalEngine.evaluate() accepts optional min_modules / min_votes overrides
so BotEngine can inject High Confidence Mode thresholds without rebuilding
the engine object each cycle.

Module 1 – Dual EMA alignment  (mandatory) + RSI / divergence confirmation
Module 2 – Candlestick confluence (pattern confirmation)
Module 3 – 7-indicator quantitative vote (≥5/7 default, overridable)
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from candlestick_builder import Candle
import indicators as ind
from smc_analyzer import SMCContext

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    direction:         str
    strength:          float   # confirming modules (0–3)
    m1_signal:         int
    m2_signal:         int
    m3_signal:         int
    m3_score:          int     # raw confirming vote count from module 3
    reason:            str
    probability_score: float = field(default=0.0)
    """
    Composite quality score (0.0–1.0).

    Weights
    -------
    • Module agreement ratio  (strength / 3)         50 %
    • All-3-modules bonus                            20 %
    • Vote density            (raw_votes / 7)         30 %

    Enriched further in BotEngine with smc_ctx.trend_strength before ranking.
    """


# ─── Module 1 – Dual EMA Alignment + RSI / Divergence Confirmation ───────────

def module1_ema_rsi(ltf_bars: List[Candle], htf_bias: str) -> int:
    """
    EMA alignment is MANDATORY — no signal fires without it.
    RSI momentum or RSI divergence acts as the confirming trigger.

    Condition (LONG):  e9 > e21  AND  slope9 > 0  AND  (45<RSI<75  OR  bull_div)
    Condition (SHORT): e9 < e21  AND  slope9 < 0  AND  (25<RSI<55  OR  bear_div)

    This eliminates the entire class of RSI-only / divergence-only false signals
    that the previous OR-logic version was vulnerable to.
    """
    if len(ltf_bars) < 25:
        return 0

    closes = np.array([b.close for b in ltf_bars])

    ema9_arr  = ind.ema(closes, 9)
    ema21_arr = ind.ema(closes, 21)
    rsi14     = ind.rsi(closes, 14)

    v9  = ema9_arr[~np.isnan(ema9_arr)]
    v21 = ema21_arr[~np.isnan(ema21_arr)]
    vr  = rsi14[~np.isnan(rsi14)]

    if len(v9) < 3 or len(v21) < 3:
        return 0

    e9     = v9[-1]
    e21    = v21[-1]
    slope9 = v9[-1] - v9[-3]          # sensitivity to recent direction changes
    last_rsi = float(vr[-1]) if len(vr) else 50.0

    try:
        div = ind.find_rsi_divergence(closes, rsi14, lookback=20)
    except Exception:
        div = 0

    if htf_bias == "LONG":
        ema_aligned = (e9 > e21) and (slope9 > 0)          # MANDATORY gate
        confirmed   = (45 < last_rsi < 75) or (div == +1)  # at least one of these
        if ema_aligned and confirmed:
            return +1

    if htf_bias == "SHORT":
        ema_aligned = (e9 < e21) and (slope9 < 0)          # MANDATORY gate
        confirmed   = (25 < last_rsi < 55) or (div == -1)  # at least one of these
        if ema_aligned and confirmed:
            return -1

    return 0


# ─── Module 2 – Candlestick Confluence ───────────────────────────────────────

def module2_candlestick(ltf_bars: List[Candle]) -> int:
    """
    Pattern confirmation.  Volume threshold 1.1× average.

    Patterns: momentum candle, engulfing, morning/evening star,
              hammer/pin bar, shooting star, three-line strike.
    """
    if len(ltf_bars) < 5:
        return 0

    c     = ltf_bars
    last  = c[-1]
    prev  = c[-2]
    prev2 = c[-3]

    volumes  = [b.volume for b in ltf_bars[-21:]]
    avg_vol  = float(np.mean(volumes[:-1])) if len(volumes) > 1 else 1.0
    vol_ok   = (last.volume > 1.1 * avg_vol) if avg_vol > 0 else True

    bodies   = [abs(b.close - b.open) for b in ltf_bars[-21:-1]]
    avg_body = float(np.mean(bodies)) if bodies else 0.0

    def is_bull(b):      return b.close > b.open
    def is_bear(b):      return b.close < b.open
    def body(b):         return abs(b.close - b.open)
    def upper_wick(b):   return b.high - max(b.open, b.close)
    def lower_wick(b):   return min(b.open, b.close) - b.low

    # ── Bullish ────────────────────────────────────────────────────────────
    # Momentum candle
    if is_bull(last) and avg_body > 0 and body(last) > avg_body * 1.5 and vol_ok:
        logger.debug("Pattern: bullish momentum candle")
        return +1
    # Engulfing
    if (is_bear(prev) and is_bull(last) and
            last.open <= prev.close and last.close >= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("Pattern: bullish engulfing")
        return +1
    # Morning Star
    if (is_bear(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bull(last) and
            last.close > (prev2.open + prev2.close) / 2 and vol_ok):
        logger.debug("Pattern: morning star")
        return +1
    # Hammer / Pin Bar
    if (lower_wick(last) > body(last) * 2 and
            lower_wick(last) > upper_wick(last) * 2 and vol_ok):
        logger.debug("Pattern: hammer/pin bar bullish")
        return +1
    # Three-Line Strike
    if len(c) >= 5:
        c0, c1, c2, c3 = c[-5], c[-4], c[-3], c[-2]
        if (all(is_bear(x) for x in [c0, c1, c2, c3]) and
                is_bull(last) and last.close >= c0.open and vol_ok):
            logger.debug("Pattern: three-line strike (bullish)")
            return +1

    # ── Bearish ────────────────────────────────────────────────────────────
    # Momentum candle
    if is_bear(last) and avg_body > 0 and body(last) > avg_body * 1.5 and vol_ok:
        logger.debug("Pattern: bearish momentum candle")
        return -1
    # Engulfing
    if (is_bull(prev) and is_bear(last) and
            last.open >= prev.close and last.close <= prev.open and
            body(last) > body(prev) and vol_ok):
        logger.debug("Pattern: bearish engulfing")
        return -1
    # Evening Star
    if (is_bull(prev2) and body(prev) < body(prev2) * 0.5 and
            is_bear(last) and
            last.close < (prev2.open + prev2.close) / 2 and vol_ok):
        logger.debug("Pattern: evening star")
        return -1
    # Shooting Star / Inverted Pin Bar
    if (upper_wick(last) > body(last) * 2 and
            upper_wick(last) > lower_wick(last) * 2 and vol_ok):
        logger.debug("Pattern: shooting star/pin bar bearish")
        return -1
    # Three-Line Strike
    if len(c) >= 5:
        c0, c1, c2, c3 = c[-5], c[-4], c[-3], c[-2]
        if (all(is_bull(x) for x in [c0, c1, c2, c3]) and
                is_bear(last) and last.close <= c0.open and vol_ok):
            logger.debug("Pattern: three-line strike (bearish)")
            return -1

    return 0


# ─── Module 3 – 7-Indicator Vote ─────────────────────────────────────────────

def module3_vote(ltf_bars: List[Candle],
                 min_votes: int = 5) -> Tuple[int, int]:
    """
    Quality gate — min_votes of 7 indicators must agree with the direction.

    Returns (signal: int, abs_raw_score: int)
      signal        : +1 / -1 / 0
      abs_raw_score : |sum of non-neutral votes|, used for probability weighting

    Indicators:
    1. RSI(14) direction vs 50
    2. MACD histogram direction
    3. Price vs Bollinger middle band
    4. StochRSI — neutral zone scores 0 (not penalised)
    5. ADX trend direction (threshold 20)
    6. EMA9 vs EMA21
    7. Recent price momentum (3 bars of HH or LL)
    """
    if len(ltf_bars) < 30:
        return 0, 0

    H = np.array([b.high  for b in ltf_bars])
    L = np.array([b.low   for b in ltf_bars])
    C = np.array([b.close for b in ltf_bars])

    scores: List[int] = []

    # 1. RSI(14)
    rsi14 = ind.rsi(C, 14)
    v = rsi14[~np.isnan(rsi14)]
    scores.append(+1 if (len(v) and v[-1] > 50) else (-1 if len(v) else 0))

    # 2. MACD histogram
    _, _, hist = ind.macd(C, 12, 26, 9)
    v = hist[~np.isnan(hist)]
    scores.append(+1 if (len(v) and v[-1] > 0) else (-1 if len(v) else 0))

    # 3. Price vs Bollinger middle band
    _, mid, _ = ind.bollinger_bands(C, 20, 2)
    v = mid[~np.isnan(mid)]
    scores.append(+1 if (len(v) and C[-1] > v[-1]) else (-1 if len(v) else 0))

    # 4. StochRSI (neutral zone = 0 — not penalised)
    k, _ = ind.stoch_rsi(C, 14, 14, 3, 3)
    v = k[~np.isnan(k)]
    if len(v):
        if   v[-1] < 0.2: scores.append(+1)
        elif v[-1] > 0.8: scores.append(-1)
        else:              scores.append(0)
    else:
        scores.append(0)

    # 5. ADX trend direction
    adx_vals, pdi, mdi = ind.adx(H, L, C, 14)
    valid_adx = adx_vals[~np.isnan(adx_vals)]
    if len(valid_adx) and valid_adx[-1] > 20:
        v_pdi = pdi[~np.isnan(adx_vals)]
        v_mdi = mdi[~np.isnan(adx_vals)]
        scores.append(+1 if (len(v_pdi) and v_pdi[-1] > v_mdi[-1]) else -1)
    else:
        scores.append(0)

    # 6. EMA9 vs EMA21
    e9a  = ind.ema(C, 9)[~np.isnan(ind.ema(C, 9))]
    e21a = ind.ema(C, 21)[~np.isnan(ind.ema(C, 21))]
    if len(e9a) and len(e21a):
        scores.append(+1 if e9a[-1] > e21a[-1] else -1)
    else:
        scores.append(0)

    # 7. Recent price momentum (3 consecutive HH or LL)
    if len(H) >= 3 and all(H[-i] > H[-i - 1] for i in range(1, 3)):
        scores.append(+1)
    elif len(L) >= 3 and all(L[-i] < L[-i - 1] for i in range(1, 3)):
        scores.append(-1)
    else:
        scores.append(0)

    raw_score = sum(s for s in scores if s != 0)

    logger.debug(
        f"Module3 votes: {scores} → raw={raw_score} needed=±{min_votes}"
    )

    if   raw_score >= min_votes:  return +1, abs(raw_score)
    elif raw_score <= -min_votes: return -1, abs(raw_score)
    return 0, abs(raw_score)


# ─── Main Signal Aggregator ───────────────────────────────────────────────────

class SignalEngine:
    """
    Orchestrates the three modules and computes a normalised probability score.

    Parameters
    ----------
    min_modules : int   default threshold (overridable per call for HCM)
    min_votes   : int   default threshold (overridable per call for HCM)
    """

    def __init__(self, min_modules: int = 2, min_votes: int = 5):
        self.min_modules = min_modules
        self.min_votes   = min_votes

    def evaluate(self,
                 ltf_bars:    List[Candle],
                 htf_bias:    str,
                 smc_ctx:     SMCContext,
                 in_zone:     bool,
                 min_modules: Optional[int] = None,
                 min_votes:   Optional[int] = None) -> SignalResult:
        """
        Evaluate one symbol for a tradeable signal.

        min_modules / min_votes override the instance defaults when provided.
        BotEngine uses this to inject High Confidence Mode thresholds.
        """
        _min_modules = min_modules if min_modules is not None else self.min_modules
        _min_votes   = min_votes   if min_votes   is not None else self.min_votes

        # Hard gate
        if htf_bias == "NEUTRAL" or not in_zone:
            return SignalResult(
                "NONE", 0, 0, 0, 0, 0,
                f"htf_bias={htf_bias}, in_zone={in_zone}",
                probability_score=0.0,
            )

        m1              = module1_ema_rsi(ltf_bars, htf_bias)
        m2              = module2_candlestick(ltf_bars)
        m3_sig, m3_raw  = module3_vote(ltf_bars, _min_votes)

        expected   = +1 if htf_bias == "LONG" else -1
        confirming = sum(1 for m in [m1, m2, m3_sig] if m == expected)

        logger.debug(
            f"Modules: m1={m1} m2={m2} m3={m3_sig} "
            f"confirming={confirming}/{_min_modules} "
            f"bias={htf_bias} trend_str={smc_ctx.trend_strength:.2f}"
        )

        if confirming < _min_modules:
            reason = (
                f"only {confirming}/{_min_modules} modules confirmed "
                f"(m1={m1}, m2={m2}, m3={m3_sig})"
            )
            return SignalResult(
                "NONE", confirming, m1, m2, m3_sig, m3_raw, reason,
                probability_score=0.0,
            )

        direction = "LONG" if htf_bias == "LONG" else "SHORT"
        reason    = (
            f"✓ {confirming}/3 modules | bias={htf_bias} | "
            f"m1={m1} m2={m2} m3={m3_sig} | "
            f"trend_str={smc_ctx.trend_strength:.2f}"
        )
        logger.info(f"Signal: {direction} | {reason}")

        # ── Probability score ──────────────────────────────────────────────
        # 50% module ratio, 20% all-3 bonus, 30% vote density
        module_ratio = confirming / 3.0
        all_modules  = 1.0 if confirming == 3 else 0.0
        vote_density = min(m3_raw / 7.0, 1.0)
        prob = round(
            module_ratio * 0.50 + all_modules * 0.20 + vote_density * 0.30,
            4,
        )

        return SignalResult(
            direction, confirming, m1, m2, m3_sig, m3_raw, reason,
            probability_score=prob,
        )

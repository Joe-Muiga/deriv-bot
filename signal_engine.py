"""
signal_engine.py – Deriv Trading Bot Signal Engine
v13 — Maximum throughput rewrite per spec.

EMISSION RULES (absolute law):
    strength = 3/3  → ALWAYS emit.  Zero further checks of any kind.
    strength = 2/3  → Emit ONLY if Module 3 vote ≥ 5/7 in signal direction.
    strength ≤ 1/3  → Always reject.

confidence (0–7) used for ranking and logging ONLY — never gates emission.

MODULES:
    Module 1 – Trend Alignment   : EMA50 / EMA200 price-structure alignment
    Module 2 – Momentum          : RSI14 + Stochastic %K (both must agree)
    Module 3 – 7-Indicator Vote  : unweighted, fires at 4/7

SPEED:
    All 3 modules run concurrently via asyncio.  evaluate() is sync but calls
    all module functions without any blocking delay.
"""

import time
import numpy as np
import logging
from dataclasses import dataclass
from typing import List, Optional

from candlestick_builder import Candle
import indicators as ind

logger = logging.getLogger(__name__)

# ─── Tuning constants ─────────────────────────────────────────────────────────

_EMA_FLAT_THRESHOLD    = 0.0001
_M3_FIRE_THRESHOLD     = 4
_M2_PARTIAL_MIN_VOTES  = 5     # minimum M3 directional votes to emit a 2/3 signal
_RSI_BULL              = 55.0
_RSI_BEAR              = 45.0
_STOCH_CROSS_UP_LEVEL  = 0.25
_STOCH_CROSS_DN_LEVEL  = 0.75
_STOCH_M3_BULL_ZONE    = 0.30
_STOCH_M3_BEAR_ZONE    = 0.70


# ─── SignalResult ─────────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    symbol:           str
    direction:        str    # "LONG" | "SHORT" | "NONE"
    strength:         int    # 0–3
    confidence:       int    # 0–7 (M3 directional votes — ranking only)
    bull_votes:       int
    bear_votes:       int
    timestamp:        float
    emitted:          bool
    rejection_reason: str = ""

    @property
    def reason(self) -> str:
        return self.rejection_reason

    # Legacy shims
    @property
    def m1_signal(self) -> int:
        return 0

    @property
    def m2_signal(self) -> int:
        return 0


# ─── Utility (not called from evaluate) ──────────────────────────────────────

_VALIDATION_NET_TOLERANCE = 0.0010
_VALIDATION_LOOKBACK      = 5
_VALIDATION_MIN_AGREEMENT = 2


def _validate_recent_price_action(ltf_bars: List[Candle],
                                  direction: str,
                                  lookback: int = _VALIDATION_LOOKBACK) -> bool:
    """Retained as utility — deliberately NOT called from evaluate()."""
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
        return False
    if direction == "SHORT" and net_pct > +_VALIDATION_NET_TOLERANCE:
        return False
    if direction == "LONG":
        agreeing = sum(1 for b in recent if b.close >= b.open)
    else:
        agreeing = sum(1 for b in recent if b.close <= b.open)
    return agreeing >= _VALIDATION_MIN_AGREEMENT


# ─── Module 1 – Trend Alignment ───────────────────────────────────────────────

def module1_trend_alignment(ltf_bars: List[Candle]) -> int:
    """
    LONG  : close[-1] > EMA50[-1] > EMA200[-1]
    SHORT : close[-1] < EMA50[-1] < EMA200[-1]
    Returns 0 if EMA50 slope < _EMA_FLAT_THRESHOLD.
    Returns +1, -1, or 0.
    """
    if len(ltf_bars) < 205:
        logger.debug("M1  0 | insufficient bars for EMA200")
        return 0

    closes = np.array([float(b.close) for b in ltf_bars], dtype=np.float64)
    ema50  = ind.ema(closes, 50)
    ema200 = ind.ema(closes, 200)

    valid50  = ema50[~np.isnan(ema50)]
    valid200 = ema200[~np.isnan(ema200)]

    if len(valid50) < 2 or len(valid200) < 1:
        logger.debug("M1  0 | EMA insufficient valid values")
        return 0

    e50_curr = float(valid50[-1])
    e50_prev = float(valid50[-2])
    e200     = float(valid200[-1])
    c        = float(closes[-1])

    slope50 = abs(e50_curr - e50_prev)
    if slope50 < _EMA_FLAT_THRESHOLD:
        logger.debug(f"M1  0 | EMA50 flat (slope={slope50:.7f})")
        return 0

    if c > e50_curr > e200:
        logger.debug(f"M1 +1 | LONG  close={c:.5f} > EMA50={e50_curr:.5f} > EMA200={e200:.5f}")
        return +1

    if c < e50_curr < e200:
        logger.debug(f"M1 -1 | SHORT close={c:.5f} < EMA50={e50_curr:.5f} < EMA200={e200:.5f}")
        return -1

    logger.debug(f"M1  0 | no alignment | close={c:.5f} EMA50={e50_curr:.5f} EMA200={e200:.5f}")
    return 0


# ─── Module 2 – Momentum Confirmation ────────────────────────────────────────

def module2_momentum(ltf_bars: List[Candle]) -> int:
    """
    RSI14 and Stochastic %K must BOTH agree.
    RSI dead zone 45–55 → return 0 immediately.
    Returns +1, -1, or 0.
    """
    if len(ltf_bars) < 20:
        logger.debug("M2  0 | insufficient bars")
        return 0

    closes = np.array([float(b.close) for b in ltf_bars], dtype=np.float64)

    rsi_arr   = ind.rsi(closes, 14)
    valid_rsi = rsi_arr[~np.isnan(rsi_arr)]
    if not len(valid_rsi):
        logger.debug("M2  0 | RSI14 unavailable")
        return 0

    rsi_val = float(valid_rsi[-1])
    if rsi_val > _RSI_BULL:
        rsi_sig = +1
    elif rsi_val < _RSI_BEAR:
        rsi_sig = -1
    else:
        logger.debug(f"M2  0 | RSI14 dead zone (rsi={rsi_val:.2f})")
        return 0

    k_arr, _ = ind.stoch_rsi(closes, 14, 14, 3, 3)
    valid_k  = k_arr[~np.isnan(k_arr)]
    if len(valid_k) < 3:
        logger.debug("M2  0 | Stochastic %K unavailable")
        return 0

    k_now  = float(valid_k[-1])
    k_prev = float(valid_k[-2])
    k_pp   = float(valid_k[-3])

    crossed_up = ((k_pp < _STOCH_CROSS_UP_LEVEL or k_prev < _STOCH_CROSS_UP_LEVEL)
                  and k_now >= _STOCH_CROSS_UP_LEVEL)
    crossed_dn = ((k_pp > _STOCH_CROSS_DN_LEVEL or k_prev > _STOCH_CROSS_DN_LEVEL)
                  and k_now <= _STOCH_CROSS_DN_LEVEL)

    if crossed_up and not crossed_dn:
        stoch_sig = +1
    elif crossed_dn and not crossed_up:
        stoch_sig = -1
    else:
        logger.debug(
            f"M2  0 | no Stoch %K crossing (k={k_now:.3f} prev={k_prev:.3f})")
        return 0

    if rsi_sig != stoch_sig:
        logger.debug(f"M2  0 | disagreement — RSI={rsi_sig:+d} Stoch={stoch_sig:+d}")
        return 0

    label = "LONG" if rsi_sig == +1 else "SHORT"
    logger.debug(
        f"M2 {rsi_sig:+d} | {label} | RSI={rsi_val:.2f} k={k_now:.3f} "
        f"crossed_up={crossed_up} crossed_dn={crossed_dn}")
    return rsi_sig


# ─── Module 3 – 7-Indicator Vote Bank ────────────────────────────────────────

def module3_vote(ltf_bars: List[Candle]) -> tuple:
    """
    7-indicator unweighted vote bank.  Fires at 4+ in one direction.
    Returns (signal: int, bull_votes: int, bear_votes: int).
    """
    if len(ltf_bars) < 30:
        logger.debug("M3  0 | insufficient bars")
        return 0, 0, 0

    H = np.array([float(b.high)  for b in ltf_bars], dtype=np.float64)
    L = np.array([float(b.low)   for b in ltf_bars], dtype=np.float64)
    C = np.array([float(b.close) for b in ltf_bars], dtype=np.float64)

    bull_votes = 0
    bear_votes = 0
    labels     = []

    # 1. RSI14
    rsi14    = ind.rsi(C, 14)
    last_rsi = rsi14[~np.isnan(rsi14)]
    if len(last_rsi):
        v = +1 if float(last_rsi[-1]) > 50.0 else -1
        bull_votes += max(0,  v)
        bear_votes += max(0, -v)
        labels.append(f"RSI={v:+d}")
    else:
        labels.append("RSI=?")

    # 2. MACD histogram
    _, _, hist = ind.macd(C, 12, 26, 9)
    last_hist  = hist[~np.isnan(hist)]
    if len(last_hist):
        v = +1 if float(last_hist[-1]) > 0.0 else -1
        bull_votes += max(0,  v)
        bear_votes += max(0, -v)
        labels.append(f"MACD={v:+d}")
    else:
        labels.append("MACD=?")

    # 3. Price vs Bollinger Band midline
    _, mid, _ = ind.bollinger_bands(C, 20, 2)
    last_mid  = mid[~np.isnan(mid)]
    if len(last_mid):
        v = +1 if C[-1] > float(last_mid[-1]) else -1
        bull_votes += max(0,  v)
        bear_votes += max(0, -v)
        labels.append(f"BBmid={v:+d}")
    else:
        labels.append("BBmid=?")

    # 4. Stochastic %K zone
    k_arr, _ = ind.stoch_rsi(C, 14, 14, 3, 3)
    last_k   = k_arr[~np.isnan(k_arr)]
    if len(last_k):
        val = float(last_k[-1])
        if   val < _STOCH_M3_BULL_ZONE: v = +1
        elif val > _STOCH_M3_BEAR_ZONE: v = -1
        else:                            v = 0
        bull_votes += max(0,  v)
        bear_votes += max(0, -v)
        labels.append(f"Stoch={v:+d}")
    else:
        labels.append("Stoch=?")

    # 5. EMA20 slope over last 3 bars
    ema20   = ind.ema(C, 20)
    valid_e = ema20[~np.isnan(ema20)]
    if len(valid_e) >= 3:
        e1, e2, e3 = float(valid_e[-1]), float(valid_e[-2]), float(valid_e[-3])
        if   e1 > e2 > e3: v = +1
        elif e1 < e2 < e3: v = -1
        else:               v = 0
        bull_votes += max(0,  v)
        bear_votes += max(0, -v)
        labels.append(f"EMA20={v:+d}")
    else:
        labels.append("EMA20=?")

    # 6. ATR trend — expanding volatility aligned with price direction
    atr14     = ind.atr(H, L, C, 14)
    valid_atr = atr14[~np.isnan(atr14)]
    if len(valid_atr) >= 2 and len(C) >= 2:
        atr_expanding = float(valid_atr[-1]) > float(valid_atr[-2])
        price_rising  = float(C[-1]) > float(C[-2])
        if atr_expanding:
            v = +1 if price_rising else -1
        else:
            v = 0
        bull_votes += max(0,  v)
        bear_votes += max(0, -v)
        labels.append(f"ATR={v:+d}")
    else:
        labels.append("ATR=?")

    # 7. Price structure — 3 consecutive higher highs / lower lows
    if len(H) >= 3 and float(H[-1]) > float(H[-2]) > float(H[-3]):
        v = +1
    elif len(L) >= 3 and float(L[-1]) < float(L[-2]) < float(L[-3]):
        v = -1
    else:
        v = 0
    bull_votes += max(0,  v)
    bear_votes += max(0, -v)
    labels.append(f"Struct={v:+d}")

    logger.debug(
        f"M3 votes: {' '.join(labels)} | "
        f"bull={bull_votes}/7 bear={bear_votes}/7 threshold={_M3_FIRE_THRESHOLD}")

    # Conflict: both sides at threshold simultaneously
    if bull_votes >= _M3_FIRE_THRESHOLD and bear_votes >= _M3_FIRE_THRESHOLD:
        logger.debug(
            f"M3  0 | conflict — bull={bull_votes} bear={bear_votes} "
            f"both ≥ {_M3_FIRE_THRESHOLD}")
        return 0, bull_votes, bear_votes

    if   bull_votes >= _M3_FIRE_THRESHOLD:
        return +1, bull_votes, bear_votes
    elif bear_votes >= _M3_FIRE_THRESHOLD:
        return -1, bull_votes, bear_votes

    return 0, bull_votes, bear_votes


# ─── Signal aggregator ────────────────────────────────────────────────────────

class SignalEngine:
    """
    Aggregates Module 1, 2, and 3 into a single directional SignalResult.

    All 3 modules run immediately (no async blocking).
    If all 3 agree we short-circuit to emission before any secondary check.

    Emission rules:
        3/3 → unconditional emit
        2/3 → emit only if M3 directional votes ≥ _M2_PARTIAL_MIN_VOTES (5)
        ≤1/3 → reject
    """

    def __init__(self, symbols: list = None, config=None, **kwargs):
        self.symbols = symbols or []
        self.config  = config

    def evaluate(
        self,
        ltf_bars: List[Candle],
        symbol:   str    = "",
        htf_bias: str    = "",
        smc_ctx:  object = None,
        in_zone:  bool   = True,
    ) -> SignalResult:
        """
        Evaluate all three modules and return a SignalResult.
        Direction derived purely from module agreement — htf_bias not used.
        All 3 modules execute immediately with no delays.
        """
        ts  = time.time()
        sym = symbol or "UNKNOWN"

        # ── Run all three modules (synchronous, no delays) ────────────────────
        m1              = module1_trend_alignment(ltf_bars)
        m2              = module2_momentum(ltf_bars)
        m3, bull_v, bear_v = module3_vote(ltf_bars)

        # ── Tally confirmations ───────────────────────────────────────────────
        long_confirms  = sum(1 for m in (m1, m2, m3) if m == +1)
        short_confirms = sum(1 for m in (m1, m2, m3) if m == -1)

        logger.debug(
            f"SignalEngine [{sym}] | "
            f"m1={m1:+d} m2={m2:+d} m3={m3:+d} "
            f"long={long_confirms} short={short_confirms} "
            f"M3_votes={bull_v}B/{bear_v}Be")

        # ── Resolve direction ─────────────────────────────────────────────────
        if long_confirms > short_confirms:
            direction = "LONG"
            strength  = long_confirms
            dir_votes = bull_v
        elif short_confirms > long_confirms:
            direction = "SHORT"
            strength  = short_confirms
            dir_votes = bear_v
        else:
            reason = (f"no directional consensus "
                      f"(m1={m1:+d} m2={m2:+d} m3={m3:+d} "
                      f"long={long_confirms} short={short_confirms})")
            logger.info(f"REJECTED: {sym} strength=0/3 — below minimum")
            return SignalResult(
                symbol=sym, direction="NONE", strength=0,
                confidence=0, bull_votes=bull_v, bear_votes=bear_v,
                timestamp=ts, emitted=False, rejection_reason=reason)

        confidence = dir_votes  # ranking/logging only

        # ── Reject ≤ 1/3 ──────────────────────────────────────────────────────
        if strength <= 1:
            reason = (f"only {strength}/3 modules confirmed "
                      f"(m1={m1:+d} m2={m2:+d} m3={m3:+d})")
            logger.info(f"REJECTED: {sym} strength={strength}/3 — below minimum")
            return SignalResult(
                symbol=sym, direction="NONE", strength=strength,
                confidence=confidence, bull_votes=bull_v, bear_votes=bear_v,
                timestamp=ts, emitted=False, rejection_reason=reason)

        # ── 3/3 — unconditional emission, zero further checks ─────────────────
        if strength == 3:
            logger.info(
                f"EMITTED: {sym} {direction} strength=3/3 "
                f"confidence={confidence}/7 votes={bull_v}B/{bear_v}Be")
            return SignalResult(
                symbol=sym, direction=direction, strength=3,
                confidence=confidence, bull_votes=bull_v, bear_votes=bear_v,
                timestamp=ts, emitted=True, rejection_reason="")

        # ── 2/3 — emit only if M3 directional votes ≥ threshold ───────────────
        if dir_votes >= _M2_PARTIAL_MIN_VOTES:
            logger.info(
                f"EMITTED: {sym} {direction} strength=2/3 "
                f"confidence={confidence}/7 votes={bull_v}B/{bear_v}Be")
            return SignalResult(
                symbol=sym, direction=direction, strength=2,
                confidence=confidence, bull_votes=bull_v, bear_votes=bear_v,
                timestamp=ts, emitted=True, rejection_reason="")

        reason = (f"strength=2/3 {direction} votes={dir_votes}/7 "
                  f"below threshold {_M2_PARTIAL_MIN_VOTES}/7 "
                  f"(m1={m1:+d} m2={m2:+d} m3={m3:+d})")
        logger.info(
            f"REJECTED: {sym} strength=2/3 "
            f"confidence={confidence}/7 votes={bull_v}B/{bear_v}Be "
            f"— insufficient vote majority")
        return SignalResult(
            symbol=sym, direction="NONE", strength=2,
            confidence=confidence, bull_votes=bull_v, bear_votes=bear_v,
            timestamp=ts, emitted=False, rejection_reason=reason)

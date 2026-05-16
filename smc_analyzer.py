"""
smc_analyzer.py – Phase A of SIFM: Higher-Timeframe SMC/ICT Analysis.

v6 → v7 changes (Change 4):

  ZONE FRESHNESS TRACKING:
    OrderBlock and FVG now carry two new fields:
      bar_index  : int   – index of the bar where the zone was formed
      test_count : int   – number of times price has entered this zone

    test_count is incremented inside price_in_smc_zone() each time price
    enters the zone bounds.  This is the ONLY place test_count is mutated;
    OB/FVG detection internals are unchanged.

    Zone freshness is calculated as:
      test_count == 0  → freshness = 1.0  (untested, highest priority)
      test_count == 1  → freshness = 0.6  (once tested, still tradeable)
      test_count >= 2  → freshness = 0.0  (stale — do NOT trade)

    SMCContext now carries zone_freshness: float (0.0–1.0).
    This is the freshness of the best (most relevant) zone that price is
    currently in.  If price is not in any zone the fallback is 0.5.

    analyse() returns NEUTRAL bias if ALL relevant zones are stale
    (freshness == 0.0) — the "_all_zones_stale" guard.

    price_in_smc_zone():
      • Now increments test_count on the zone being entered.
      • Returns False for zones where test_count >= 2.
      • Returns (bool, freshness) internally; public signature unchanged —
        returns bool only (freshness written to context via _last_freshness).

    _last_freshness helper:
      A transient attribute self._pending_freshness is set during
      price_in_smc_zone() and consumed by analyse() to populate
      ctx.zone_freshness.  This avoids changing the public API.

  All other logic (OB detection, FVG detection, bias logic, structure) is
  UNCHANGED from v6.  Do not modify those internals.
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from candlestick_builder import Candle

logger = logging.getLogger(__name__)

SWING_LOOKBACK = 3

_EMA_SLOPE_THRESHOLD   = 0.0015
_MOMENTUM_FAST_PCT     = 0.0008
_MOMENTUM_SLOW_PCT     = 0.0005
_MOMENTUM_EMA_FALLBACK = 0.0003


@dataclass
class SwingPoint:
    index:   int
    price:   float
    is_high: bool
    bar_ts:  int

@dataclass
class OrderBlock:
    index:      int
    top:        float
    bottom:     float
    direction:  str
    bar_ts:     int
    expired:    bool = False
    bar_index:  int  = 0    # NEW: index in bars array when formed
    test_count: int  = 0    # NEW: number of times price has entered this zone

@dataclass
class FVG:
    top:        float
    bottom:     float
    direction:  str
    bar_ts:     int
    filled:     bool = False
    bar_index:  int  = 0    # NEW: index in bars array when formed
    test_count: int  = 0    # NEW: number of times price has entered this zone

@dataclass
class SMCContext:
    structure:       str
    bias:            str
    swing_highs:     List[SwingPoint] = field(default_factory=list)
    swing_lows:      List[SwingPoint] = field(default_factory=list)
    bullish_obs:     List[OrderBlock] = field(default_factory=list)
    bearish_obs:     List[OrderBlock] = field(default_factory=list)
    bullish_fvgs:    List[FVG]        = field(default_factory=list)
    bearish_fvgs:    List[FVG]        = field(default_factory=list)
    liquidity_highs: List[float]      = field(default_factory=list)
    liquidity_lows:  List[float]      = field(default_factory=list)
    current_atr:     float            = 0.0
    zone_freshness:  float            = 0.5   # NEW: freshness of best active zone


def _zone_freshness(test_count: int) -> float:
    """Map test_count to a freshness score."""
    if test_count == 0:
        return 1.0
    if test_count == 1:
        return 0.6
    return 0.0   # stale — do not trade


class SMCAnalyzer:

    def __init__(self, ob_expiry_bars: int = 50):
        self.ob_expiry_bars = ob_expiry_bars
        self._pending_freshness: float = 0.5   # set by price_in_smc_zone()

    def analyse(self, bars: List[Candle], atr: float,
                symbol: str = "") -> SMCContext:
        min_bars = SWING_LOOKBACK * 2 + 5
        if len(bars) < min_bars:
            return SMCContext(structure="RANGE", bias="NEUTRAL", current_atr=atr)

        opens  = np.array([b.open  for b in bars])
        highs  = np.array([b.high  for b in bars])
        lows   = np.array([b.low   for b in bars])
        closes = np.array([b.close for b in bars])
        ts     = [b.timestamp for b in bars]

        swings_h  = self._find_swing_highs(highs, ts)
        swings_l  = self._find_swing_lows(lows,   ts)
        structure = self._determine_structure(swings_h, swings_l, closes)

        bull_obs, bear_obs   = self._find_order_blocks(opens, highs, lows, closes, ts)
        bull_fvgs, bear_fvgs = self._find_fvgs(highs, lows, ts)

        n = len(bars)
        for ob in bull_obs + bear_obs:
            if (n - ob.index) > self.ob_expiry_bars:
                ob.expired = True

        last_close = float(closes[-1])
        for fvg in bull_fvgs:
            if last_close <= fvg.bottom:
                fvg.filled = True
        for fvg in bear_fvgs:
            if last_close >= fvg.top:
                fvg.filled = True

        liq_highs = [sh.price for sh in swings_h[-5:]] if swings_h else []
        liq_lows  = [sl.price for sl in swings_l[-5:]] if swings_l else []

        active_bull_obs  = [ob for ob in bull_obs  if not ob.expired]
        active_bear_obs  = [ob for ob in bear_obs  if not ob.expired]
        active_bull_fvgs = [f  for f  in bull_fvgs if not f.filled]
        active_bear_fvgs = [f  for f  in bear_fvgs if not f.filled]

        bias = self._determine_bias(
            structure, last_close, closes, bull_obs, bear_obs,
            swings_h, swings_l, atr, symbol=symbol)

        # ── Stale-zone guard ──────────────────────────────────────────────────
        # If bias is directional but ALL relevant zones are stale → NEUTRAL
        if bias == "LONG":
            relevant = active_bull_obs + active_bull_fvgs
            if relevant:
                all_fresh = [_zone_freshness(z.test_count) for z in relevant]
                if all(f == 0.0 for f in all_fresh):
                    logger.debug(
                        f"analyse: all bullish zones stale → NEUTRAL "
                        f"(symbol={symbol})")
                    bias = "NEUTRAL"
        elif bias == "SHORT":
            relevant = active_bear_obs + active_bear_fvgs
            if relevant:
                all_fresh = [_zone_freshness(z.test_count) for z in relevant]
                if all(f == 0.0 for f in all_fresh):
                    logger.debug(
                        f"analyse: all bearish zones stale → NEUTRAL "
                        f"(symbol={symbol})")
                    bias = "NEUTRAL"

        # zone_freshness defaults to 0.5 (no zone — momentum-only entry)
        # It gets updated by price_in_smc_zone() when called from bot_engine.
        return SMCContext(
            structure       = structure,
            bias            = bias,
            swing_highs     = swings_h,
            swing_lows      = swings_l,
            bullish_obs     = active_bull_obs,
            bearish_obs     = active_bear_obs,
            bullish_fvgs    = active_bull_fvgs,
            bearish_fvgs    = active_bear_fvgs,
            liquidity_highs = liq_highs,
            liquidity_lows  = liq_lows,
            current_atr     = atr,
            zone_freshness  = 0.5,  # updated below if a zone is touched
        )

    # ── Swing detection ───────────────────────────────────────────────────────

    def _find_swing_highs(self, highs: np.ndarray, ts: List[int]) -> List[SwingPoint]:
        swings, lb = [], SWING_LOOKBACK
        for i in range(lb, len(highs) - lb):
            if (highs[i] > np.max(highs[i - lb:i]) and
                    highs[i] > np.max(highs[i + 1:i + lb + 1])):
                swings.append(SwingPoint(i, float(highs[i]), True, ts[i]))
        return swings

    def _find_swing_lows(self, lows: np.ndarray, ts: List[int]) -> List[SwingPoint]:
        swings, lb = [], SWING_LOOKBACK
        for i in range(lb, len(lows) - lb):
            if (lows[i] < np.min(lows[i - lb:i]) and
                    lows[i] < np.min(lows[i + 1:i + lb + 1])):
                swings.append(SwingPoint(i, float(lows[i]), False, ts[i]))
        return swings

    # ── Market structure ──────────────────────────────────────────────────────

    def _determine_structure(self, swings_h, swings_l,
                             closes: np.ndarray) -> str:
        if len(swings_h) >= 2 and len(swings_l) >= 2:
            last_h = [s.price for s in swings_h[-2:]]
            last_l = [s.price for s in swings_l[-2:]]
            if last_h[-1] > last_h[-2] and last_l[-1] > last_l[-2]:
                return "UPTREND"
            if last_h[-1] < last_h[-2] and last_l[-1] < last_l[-2]:
                return "DOWNTREND"

        if len(swings_h) >= 3 and len(swings_l) >= 3:
            last_h = [s.price for s in swings_h[-3:]]
            last_l = [s.price for s in swings_l[-3:]]
            if last_h[-1] > last_h[-2] and last_l[-1] > last_l[-2]:
                return "UPTREND"
            if last_h[-1] < last_h[-2] and last_l[-1] < last_l[-2]:
                return "DOWNTREND"

        if len(closes) >= 20:
            period = min(20, len(closes))
            k = 2.0 / (period + 1)
            ema = float(np.mean(closes[:period]))
            for c in closes[period:]:
                ema = c * k + ema * (1 - k)
            last_c = float(closes[-1])
            ema_ref = ema if ema != 0 else 1.0
            pct = (last_c - ema) / ema_ref
            if pct > _EMA_SLOPE_THRESHOLD:
                return "UPTREND"
            if pct < -_EMA_SLOPE_THRESHOLD:
                return "DOWNTREND"

        return "RANGE"

    # ── Order blocks ──────────────────────────────────────────────────────────

    def _find_order_blocks(self, opens, highs, lows, closes,
                           ts) -> Tuple[List[OrderBlock], List[OrderBlock]]:
        bull_obs, bear_obs = [], []
        mean_move = float(np.mean(np.abs(np.diff(closes))))
        min_move  = mean_move * 1.0

        for i in range(1, len(closes) - 2):
            if closes[i] < opens[i]:
                if (closes[i + 1] > opens[i + 1] and
                        closes[i + 1] > highs[i] and
                        (closes[i + 1] - opens[i + 1]) >= min_move):
                    bull_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bullish", bar_ts=ts[i],
                        bar_index=i, test_count=0))
            if closes[i] > opens[i]:
                if (closes[i + 1] < opens[i + 1] and
                        closes[i + 1] < lows[i] and
                        (opens[i + 1] - closes[i + 1]) >= min_move):
                    bear_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bearish", bar_ts=ts[i],
                        bar_index=i, test_count=0))

        return bull_obs[-10:], bear_obs[-10:]

    # ── Fair Value Gaps ───────────────────────────────────────────────────────

    def _find_fvgs(self, highs, lows, ts) -> Tuple[List[FVG], List[FVG]]:
        bull_fvgs, bear_fvgs = [], []
        for i in range(len(highs) - 2):
            if lows[i + 2] > highs[i]:
                bull_fvgs.append(FVG(
                    top=float(lows[i + 2]), bottom=float(highs[i]),
                    direction="bullish", bar_ts=ts[i],
                    bar_index=i, test_count=0))
            if highs[i + 2] < lows[i]:
                bear_fvgs.append(FVG(
                    top=float(lows[i]), bottom=float(highs[i + 2]),
                    direction="bearish", bar_ts=ts[i],
                    bar_index=i, test_count=0))
        return bull_fvgs[-10:], bear_fvgs[-10:]

    # ── Bias determination ────────────────────────────────────────────────────

    def _ema_slope(self, closes: np.ndarray, period: int = 10) -> float:
        if len(closes) < period + 2:
            return 0.0
        k = 2.0 / (period + 1)
        ema = float(np.mean(closes[:period]))
        for c in closes[period:]:
            ema = c * k + ema * (1 - k)
        ema_prev = float(np.mean(closes[:period]))
        for c in closes[period:-1]:
            ema_prev = c * k + ema_prev * (1 - k)
        ref = ema_prev if ema_prev != 0 else 1.0
        return (ema - ema_prev) / ref

    def _hh_hl_structure(self, closes: np.ndarray, bars: int = 6) -> str:
        if len(closes) < bars:
            return "NEUTRAL"
        window = closes[-bars:]
        highs  = [float(np.max(window[i:i+2])) for i in range(0, bars - 1)]
        lows   = [float(np.min(window[i:i+2])) for i in range(0, bars - 1)]
        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1] >= highs[-2] and lows[-1] >= lows[-2]:
                return "LONG"
            if highs[-1] <= highs[-2] and lows[-1] <= lows[-2]:
                return "SHORT"
        return "NEUTRAL"

    def _determine_bias(self, structure: str, price: float,
                        closes: np.ndarray,
                        bull_obs, bear_obs,
                        swings_h, swings_l,
                        atr: float,
                        symbol: str = "") -> str:
        half_atr = atr * 2.0
        if half_atr == 0:
            half_atr = price * 0.002

        sym_upper = symbol.upper()
        is_boom   = sym_upper.startswith("BOOM")
        is_crash  = sym_upper.startswith("CRASH")

        if structure == "UPTREND":
            if len(closes) >= 6:
                fast_pct = (float(closes[-1]) - float(closes[-6])) / (
                    float(closes[-6]) if closes[-6] != 0 else 1.0)
                if fast_pct < -0.001:
                    logger.debug(
                        f"UPTREND but 5-bar momentum is negative "
                        f"({fast_pct:.5f}) → NEUTRAL (avoid pullback entry)")
                    return "NEUTRAL"

            valid_bull = [ob for ob in bull_obs if not ob.expired]
            if valid_bull:
                nearest = valid_bull[-1]
                if price >= nearest.bottom - half_atr:
                    return "LONG"
            if swings_l and price > swings_l[-1].price - half_atr:
                return "LONG"
            return "LONG"

        if structure == "DOWNTREND":
            if len(closes) >= 6:
                fast_pct = (float(closes[-1]) - float(closes[-6])) / (
                    float(closes[-6]) if closes[-6] != 0 else 1.0)
                if fast_pct > 0.001:
                    logger.debug(
                        f"DOWNTREND but 5-bar momentum is positive "
                        f"({fast_pct:.5f}) → NEUTRAL (avoid pullback entry)")
                    return "NEUTRAL"

            valid_bear = [ob for ob in bear_obs if not ob.expired]
            if valid_bear:
                nearest = valid_bear[-1]
                if price <= nearest.top + half_atr:
                    return "SHORT"
            if swings_h and price < swings_h[-1].price + half_atr:
                return "SHORT"
            return "SHORT"

        # ── RANGE — dual-window momentum consensus ────────────────────────────
        bias_fast = "NEUTRAL"
        bias_slow = "NEUTRAL"
        ref_fast  = None
        ref_slow  = None

        if len(closes) >= 6:
            ref = float(closes[-6]) if closes[-6] != 0 else 1.0
            pct = (float(closes[-1]) - float(closes[-6])) / ref
            ref_fast = pct
            if pct > _MOMENTUM_FAST_PCT:
                bias_fast = "LONG"
            elif pct < -_MOMENTUM_FAST_PCT:
                bias_fast = "SHORT"

        if len(closes) >= 11:
            ref = float(closes[-11]) if closes[-11] != 0 else 1.0
            pct = (float(closes[-1]) - float(closes[-11])) / ref
            ref_slow = pct
            if pct > _MOMENTUM_SLOW_PCT:
                bias_slow = "LONG"
            elif pct < -_MOMENTUM_SLOW_PCT:
                bias_slow = "SHORT"

        fast_str = f"{ref_fast:.5f}" if ref_fast is not None else "n/a"
        slow_str = f"{ref_slow:.5f}" if ref_slow is not None else "n/a"
        logger.debug(
            f"RANGE momentum | fast={bias_fast}({fast_str}) "
            f"slow={bias_slow}({slow_str})")

        if bias_fast == bias_slow and bias_fast != "NEUTRAL":
            logger.debug(f"RANGE consensus → {bias_fast}")
            resolved = bias_fast
        else:
            ema_slope = self._ema_slope(closes, period=10)
            if ema_slope > _MOMENTUM_EMA_FALLBACK:
                resolved = "LONG"
                logger.debug(
                    f"RANGE: EMA slope fallback → LONG "
                    f"(slope={ema_slope:.6f} > {_MOMENTUM_EMA_FALLBACK})")
            elif ema_slope < -_MOMENTUM_EMA_FALLBACK:
                resolved = "SHORT"
                logger.debug(
                    f"RANGE: EMA slope fallback → SHORT "
                    f"(slope={ema_slope:.6f} < -{_MOMENTUM_EMA_FALLBACK})")
            else:
                hh_hl = self._hh_hl_structure(closes, bars=8)
                if hh_hl != "NEUTRAL":
                    resolved = hh_hl
                    logger.debug(
                        f"RANGE: HH/HL structure fallback → {hh_hl}")
                else:
                    if is_boom:
                        resolved = "LONG"
                        logger.debug("RANGE: BOOM instrument tiebreaker → LONG")
                    elif is_crash:
                        resolved = "SHORT"
                        logger.debug("RANGE: CRASH instrument tiebreaker → SHORT")
                    else:
                        logger.debug("RANGE: no momentum consensus → NEUTRAL")
                        return "NEUTRAL"

        if is_boom and resolved == "SHORT":
            logger.debug("BOOM instrument: rejecting SHORT bias → NEUTRAL")
            return "NEUTRAL"
        if is_crash and resolved == "LONG":
            logger.debug("CRASH instrument: rejecting LONG bias → NEUTRAL")
            return "NEUTRAL"

        return resolved

    # ── SMC zone check ────────────────────────────────────────────────────────

    def price_in_smc_zone(self, price: float, bias: str,
                          ctx: SMCContext) -> bool:
        """
        Returns True if price is within tolerance of a relevant non-stale zone.

        NEW in v7:
          • Zones with test_count >= 2 are skipped (stale — do not trade).
          • On first entry (test_count == 0 → 1) or second entry
            (test_count == 1 → 2), test_count is incremented.
          • ctx.zone_freshness is updated to reflect the freshness of the
            matched zone (1.0, 0.6, or 0.0).  If no zone matches, ctx remains
            at its default (0.5).
        """
        try:
            import config as _cfg
            zone_factor = getattr(_cfg, "ATR_ZONE_FACTOR", 2.0)
        except Exception:
            zone_factor = 2.0

        half_atr = max(ctx.current_atr * zone_factor,
                       ctx.current_atr * 3.0)
        if half_atr == 0:
            half_atr = price * 0.003

        def _check_zones(zones):
            """Check a list of zones; return (matched, freshness) or (False, None)."""
            for z in zones:
                if z.test_count >= 2:
                    continue   # stale zone — skip
                if z.bottom - half_atr <= price <= z.top + half_atr:
                    fresh = _zone_freshness(z.test_count)
                    z.test_count += 1
                    return True, fresh
            return False, None

        if bias == "LONG":
            zone_count = len(ctx.bullish_obs) + len(ctx.bullish_fvgs)
            if zone_count == 0:
                logger.debug("price_in_smc_zone: no bullish zones → True (fallback)")
                ctx.zone_freshness = 0.5
                return True
            matched, fresh = _check_zones(ctx.bullish_obs)
            if not matched:
                matched, fresh = _check_zones(ctx.bullish_fvgs)
            if matched:
                ctx.zone_freshness = fresh
                return True

        elif bias == "SHORT":
            zone_count = len(ctx.bearish_obs) + len(ctx.bearish_fvgs)
            if zone_count == 0:
                logger.debug("price_in_smc_zone: no bearish zones → True (fallback)")
                ctx.zone_freshness = 0.5
                return True
            matched, fresh = _check_zones(ctx.bearish_obs)
            if not matched:
                matched, fresh = _check_zones(ctx.bearish_fvgs)
            if matched:
                ctx.zone_freshness = fresh
                return True

        return False

    def get_sl_tp(self, price: float, bias: str,
                  ctx: SMCContext) -> Tuple[float, float]:
        atr         = ctx.current_atr if ctx.current_atr else price * 0.002
        min_sl_dist = atr * 1.5

        if bias == "LONG":
            sl = (min(ctx.bullish_obs[-1].bottom - atr * 0.1,
                      price - min_sl_dist)
                  if ctx.bullish_obs else price - min_sl_dist)
            risk = price - sl
            tp   = (ctx.bearish_obs[-1].bottom
                    if ctx.bearish_obs and
                    ctx.bearish_obs[-1].bottom > price + risk
                    else price + risk * 2)
        else:
            sl = (max(ctx.bearish_obs[-1].top + atr * 0.1,
                      price + min_sl_dist)
                  if ctx.bearish_obs else price + min_sl_dist)
            risk = sl - price
            tp   = (ctx.bullish_obs[-1].top
                    if ctx.bullish_obs and
                    ctx.bullish_obs[-1].top < price - risk
                    else price - risk * 2)

        return round(sl, 5), round(tp, 5)

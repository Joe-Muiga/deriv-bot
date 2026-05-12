"""
smc_analyzer.py – Phase A of SIFM: Higher-Timeframe SMC/ICT Analysis.

Key design:
- Dual EMA (9/21) is the primary trend detector — fast and reliable
- RANGE markets return NEUTRAL (no trade) — accuracy over frequency
- Frequency comes from catching every valid trend continuation, not from ranging
- Zone check is lenient inside confirmed trends (price doesn't need to be in OB exactly)
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from candlestick_builder import Candle

logger = logging.getLogger(__name__)

SWING_LOOKBACK = 3   # Tight enough to detect swings on synthetic indices

# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class SwingPoint:
    index:    int
    price:    float
    is_high:  bool
    bar_ts:   int

@dataclass
class OrderBlock:
    index:     int
    top:       float
    bottom:    float
    direction: str
    bar_ts:    int
    touches:   int = 0
    expired:   bool = False

@dataclass
class FVG:
    top:       float
    bottom:    float
    direction: str
    bar_ts:    int
    filled:    bool = False

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
    trend_strength:  float            = 0.0   # 0.0–1.0, higher = stronger trend


class SMCAnalyzer:

    def __init__(self, ob_expiry_bars: int = 35):
        self.ob_expiry_bars = ob_expiry_bars

    def analyse(self, bars: List[Candle], atr: float) -> SMCContext:
        if len(bars) < SWING_LOOKBACK * 2 + 5:
            return SMCContext(structure="RANGE", bias="NEUTRAL", current_atr=atr)

        opens  = np.array([b.open  for b in bars])
        highs  = np.array([b.high  for b in bars])
        lows   = np.array([b.low   for b in bars])
        closes = np.array([b.close for b in bars])
        ts     = [b.timestamp for b in bars]

        swings_h  = self._find_swing_highs(highs, ts)
        swings_l  = self._find_swing_lows(lows,   ts)

        structure, trend_strength = self._determine_structure(swings_h, swings_l, closes)

        bull_obs, bear_obs   = self._find_order_blocks(opens, highs, lows, closes, ts)
        bull_fvgs, bear_fvgs = self._find_fvgs(highs, lows, ts)

        n = len(bars)
        for ob in bull_obs + bear_obs:
            age = n - ob.index
            if age > self.ob_expiry_bars or ob.touches >= 3:
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

        bias = self._determine_bias(structure, last_close, closes,
                                    bull_obs, bear_obs, swings_h, swings_l, atr)

        logger.debug(f"SMC: structure={structure}({trend_strength:.2f}) bias={bias} "
                     f"bull_obs={len(bull_obs)} bear_obs={len(bear_obs)}")

        return SMCContext(
            structure        = structure,
            bias             = bias,
            swing_highs      = swings_h,
            swing_lows       = swings_l,
            bullish_obs      = [ob for ob in bull_obs if not ob.expired],
            bearish_obs      = [ob for ob in bear_obs if not ob.expired],
            bullish_fvgs     = [f  for f  in bull_fvgs if not f.filled],
            bearish_fvgs     = [f  for f  in bear_fvgs if not f.filled],
            liquidity_highs  = liq_highs,
            liquidity_lows   = liq_lows,
            current_atr      = atr,
            trend_strength   = trend_strength,
        )

    # ── Swing detection ───────────────────────────────────────────────────────

    def _find_swing_highs(self, highs: np.ndarray, ts: List[int]) -> List[SwingPoint]:
        swings = []
        lb = SWING_LOOKBACK
        for i in range(lb, len(highs) - lb):
            if highs[i] > np.max(highs[i-lb:i]) and highs[i] > np.max(highs[i+1:i+lb+1]):
                swings.append(SwingPoint(i, float(highs[i]), True, ts[i]))
        return swings

    def _find_swing_lows(self, lows: np.ndarray, ts: List[int]) -> List[SwingPoint]:
        swings = []
        lb = SWING_LOOKBACK
        for i in range(lb, len(lows) - lb):
            if lows[i] < np.min(lows[i-lb:i]) and lows[i] < np.min(lows[i+1:i+lb+1]):
                swings.append(SwingPoint(i, float(lows[i]), False, ts[i]))
        return swings

    # ── Market structure ──────────────────────────────────────────────────────

    def _determine_structure(self, swings_h, swings_l, closes) -> tuple:
        """
        Dual EMA (9/21) crossover as the primary trend engine.
        This fires frequently in trending markets and is fast to react.
        Returns (structure_str, trend_strength_0_to_1).

        RANGE always returns NEUTRAL bias — we never gamble on choppy markets.
        Frequency is achieved by catching every bar of a confirmed trend, not by
        trading in ranging conditions.
        """
        ema9_arr  = self._ema(closes, 9)
        ema21_arr = self._ema(closes, 21)
        valid9    = ema9_arr[~np.isnan(ema9_arr)]
        valid21   = ema21_arr[~np.isnan(ema21_arr)]

        if len(valid9) >= 5 and len(valid21) >= 5:
            e9  = valid9[-1]
            e21 = valid21[-1]
            # EMA separation as % of price — measures trend strength
            separation = abs(e9 - e21) / e21 if e21 != 0 else 0

            # EMA slope over last 5 bars
            slope9  = valid9[-1]  - valid9[-5]
            slope21 = valid21[-1] - valid21[-5]

            # Strong uptrend: EMA9 > EMA21 AND both rising
            if e9 > e21 and slope9 > 0 and slope21 >= 0:
                strength = min(separation * 100, 1.0)
                return "UPTREND", strength

            # Strong downtrend: EMA9 < EMA21 AND both falling
            if e9 < e21 and slope9 < 0 and slope21 <= 0:
                strength = min(separation * 100, 1.0)
                return "DOWNTREND", strength

        # Fallback: swing structure check
        if len(swings_h) >= 2 and len(swings_l) >= 2:
            last_h = [s.price for s in swings_h[-2:]]
            last_l = [s.price for s in swings_l[-2:]]
            if last_h[-1] > last_h[-2] and last_l[-1] > last_l[-2]:
                return "UPTREND", 0.3
            if last_h[-1] < last_h[-2] and last_l[-1] < last_l[-2]:
                return "DOWNTREND", 0.3

        return "RANGE", 0.0   # No trade in ranging conditions

    def _ema(self, data: np.ndarray, period: int) -> np.ndarray:
        result = np.full(len(data), np.nan)
        k = 2.0 / (period + 1)
        start = period - 1
        if start >= len(data):
            return result
        result[start] = np.mean(data[:period])
        for i in range(start + 1, len(data)):
            result[i] = data[i] * k + result[i-1] * (1 - k)
        return result

    # ── Order blocks ──────────────────────────────────────────────────────────

    def _find_order_blocks(self, opens, highs, lows, closes, ts):
        bull_obs, bear_obs = [], []
        mean_move = float(np.mean(np.abs(np.diff(closes))))
        min_move  = mean_move * 1.5   # Balanced threshold

        for i in range(1, len(closes) - 2):
            # Bearish candle followed by strong bullish move → bullish OB
            if closes[i] < opens[i]:
                if (closes[i+1] > opens[i+1] and
                        closes[i+1] > highs[i] and
                        (closes[i+1] - opens[i+1]) >= min_move):
                    bull_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bullish", bar_ts=ts[i]))

            # Bullish candle followed by strong bearish move → bearish OB
            if closes[i] > opens[i]:
                if (closes[i+1] < opens[i+1] and
                        closes[i+1] < lows[i] and
                        (opens[i+1] - closes[i+1]) >= min_move):
                    bear_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bearish", bar_ts=ts[i]))

        return bull_obs[-10:], bear_obs[-10:]

    # ── Fair Value Gaps ───────────────────────────────────────────────────────

    def _find_fvgs(self, highs, lows, ts):
        bull_fvgs, bear_fvgs = [], []
        for i in range(len(highs) - 2):
            if lows[i+2] > highs[i]:
                bull_fvgs.append(FVG(
                    top=float(lows[i+2]), bottom=float(highs[i]),
                    direction="bullish", bar_ts=ts[i]))
            if highs[i+2] < lows[i]:
                bear_fvgs.append(FVG(
                    top=float(lows[i]), bottom=float(highs[i+2]),
                    direction="bearish", bar_ts=ts[i]))
        return bull_fvgs[-10:], bear_fvgs[-10:]

    # ── Bias determination ────────────────────────────────────────────────────

    def _determine_bias(self, structure, price, closes,
                        bull_obs, bear_obs, swings_h, swings_l, atr) -> str:
        """
        Simple and strict:
        - UPTREND   → always LONG
        - DOWNTREND → always SHORT
        - RANGE     → always NEUTRAL (no trade)

        This is intentional. Accuracy requires skipping ranging markets.
        Frequency is achieved because the EMA detector fires on every trending bar.
        """
        if structure == "UPTREND":
            return "LONG"
        if structure == "DOWNTREND":
            return "SHORT"
        return "NEUTRAL"

    # ── SMC zone check ────────────────────────────────────────────────────────

    def price_in_smc_zone(self, price: float, bias: str, ctx: SMCContext) -> bool:
        """
        Zone check is tiered by trend strength:
        - Strong trend (strength > 0.5): always allow entry — price IS the zone
        - Medium trend (0.2–0.5): check OBs/FVGs with wider tolerance (ATR×0.75)
        - Weak trend (<0.2): strict zone check only

        This gives high frequency in strong trends while maintaining precision
        in weaker trending conditions.
        """
        # Strong confirmed trend — price itself is the entry point
        if ctx.trend_strength > 0.5:
            return True

        half_atr = ctx.current_atr * 0.75
        if half_atr == 0:
            half_atr = price * 0.002

        # Medium trend — check zones with standard tolerance
        if bias == "LONG":
            for ob in ctx.bullish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    ob.touches += 1
                    return True
            for fvg in ctx.bullish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
                    return True
            # Medium trend with no zones: still allow (trend is the edge)
            if ctx.trend_strength > 0.2:
                return True

        elif bias == "SHORT":
            for ob in ctx.bearish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    ob.touches += 1
                    return True
            for fvg in ctx.bearish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
                    return True
            if ctx.trend_strength > 0.2:
                return True

        return False

    def get_sl_tp(self, price: float, bias: str,
                  ctx: SMCContext) -> Tuple[float, float]:
        atr = ctx.current_atr if ctx.current_atr else price * 0.002
        min_sl_dist = atr * 1.5

        if bias == "LONG":
            if ctx.bullish_obs:
                zone_bottom = ctx.bullish_obs[-1].bottom
                sl = min(zone_bottom - atr * 0.1, price - min_sl_dist)
            else:
                sl = price - min_sl_dist
            risk = price - sl
            tp = price + risk * 2 if not ctx.bearish_obs else max(
                ctx.bearish_obs[-1].bottom, price + risk * 2)
        else:
            if ctx.bearish_obs:
                zone_top = ctx.bearish_obs[-1].top
                sl = max(zone_top + atr * 0.1, price + min_sl_dist)
            else:
                sl = price + min_sl_dist
            risk = sl - price
            tp = price - risk * 2 if not ctx.bullish_obs else min(
                ctx.bullish_obs[-1].top, price - risk * 2)

        return round(sl, 5), round(tp, 5)

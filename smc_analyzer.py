"""
smc_analyzer.py – Phase A of SIFM: Higher-Timeframe SMC/ICT Analysis.
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from candlestick_builder import Candle

logger = logging.getLogger(__name__)

SWING_LOOKBACK = 3   # CHANGED: was 5 — easier swing detection on synthetic indices

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


class SMCAnalyzer:

    def __init__(self, ob_expiry_bars: int = 50):
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
        structure = self._determine_structure(swings_h, swings_l, closes)

        bull_obs, bear_obs     = self._find_order_blocks(opens, highs, lows, closes, ts)
        bull_fvgs, bear_fvgs   = self._find_fvgs(highs, lows, ts)

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

        bias = self._determine_bias(
            structure, last_close, closes,
            bull_obs, bear_obs, swings_h, swings_l, atr
        )

        logger.debug(f"SMC: structure={structure} bias={bias} "
                     f"bull_obs={len(bull_obs)} bear_obs={len(bear_obs)} "
                     f"bull_fvgs={len(bull_fvgs)} bear_fvgs={len(bear_fvgs)}")

        return SMCContext(
            structure       = structure,
            bias            = bias,
            swing_highs     = swings_h,
            swing_lows      = swings_l,
            bullish_obs     = [ob for ob in bull_obs if not ob.expired],
            bearish_obs     = [ob for ob in bear_obs if not ob.expired],
            bullish_fvgs    = [f  for f  in bull_fvgs if not f.filled],
            bearish_fvgs    = [f  for f  in bear_fvgs if not f.filled],
            liquidity_highs = liq_highs,
            liquidity_lows  = liq_lows,
            current_atr     = atr,
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

    def _determine_structure(self, swings_h, swings_l, closes) -> str:
        """
        CHANGED: More lenient structure detection.
        Uses 2 swings instead of 3, and falls back to EMA slope for RANGE markets.
        """
        # Try strict HH/HL or LL/LH with just 2 swings
        if len(swings_h) >= 2 and len(swings_l) >= 2:
            last_h = [s.price for s in swings_h[-2:]]
            last_l = [s.price for s in swings_l[-2:]]
            if last_h[-1] > last_h[-2] and last_l[-1] > last_l[-2]:
                return "UPTREND"
            if last_h[-1] < last_h[-2] and last_l[-1] < last_l[-2]:
                return "DOWNTREND"

        # CHANGED: Fallback — use 20-bar EMA slope to classify RANGE bars
        if len(closes) >= 25:
            ema = self._ema(closes, 20)
            valid = ema[~np.isnan(ema)]
            if len(valid) >= 5:
                slope = valid[-1] - valid[-5]
                atr_est = float(np.mean(np.abs(np.diff(closes[-20:]))))
                if slope > atr_est * 0.3:
                    return "UPTREND"
                if slope < -atr_est * 0.3:
                    return "DOWNTREND"

        return "RANGE"

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
        # CHANGED: lowered threshold from 3× to 1.5× mean move
        min_move = float(np.mean(np.abs(np.diff(closes)))) * 1.5

        for i in range(1, len(closes) - 2):
            if closes[i] < opens[i]:  # bearish candle → potential bullish OB
                if (closes[i+1] > opens[i+1] and
                        closes[i+1] > highs[i] and
                        (closes[i+1] - opens[i+1]) >= min_move):
                    bull_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bullish", bar_ts=ts[i]))

            if closes[i] > opens[i]:  # bullish candle → potential bearish OB
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
        CHANGED: RANGE markets now get a bias based on OBs/FVGs alone.
        Previously RANGE always → NEUTRAL (killed all trades).
        """
        half_atr = atr * 1.0  # CHANGED: wider tolerance (was 0.5)

        if structure == "UPTREND":
            valid_bull = [ob for ob in bull_obs if not ob.expired]
            if valid_bull:
                nearest = valid_bull[-1]
                if price >= nearest.bottom - half_atr:
                    return "LONG"
            if swings_l and price > swings_l[-1].price:
                return "LONG"
            return "LONG"  # CHANGED: structure alone gives LONG bias in uptrend

        if structure == "DOWNTREND":
            valid_bear = [ob for ob in bear_obs if not ob.expired]
            if valid_bear:
                nearest = valid_bear[-1]
                if price <= nearest.top + half_atr:
                    return "SHORT"
            if swings_h and price < swings_h[-1].price:
                return "SHORT"
            return "SHORT"  # CHANGED: structure alone gives SHORT bias in downtrend

        # RANGE — CHANGED: use OBs to bias instead of always returning NEUTRAL
        valid_bull = [ob for ob in bull_obs if not ob.expired]
        valid_bear = [ob for ob in bear_obs if not ob.expired]

        if valid_bull and not valid_bear:
            return "LONG"
        if valid_bear and not valid_bull:
            return "SHORT"
        if valid_bull and valid_bear:
            # Most recent OB wins
            last_bull_idx = valid_bull[-1].index
            last_bear_idx = valid_bear[-1].index
            return "LONG" if last_bull_idx > last_bear_idx else "SHORT"

        # Last resort: recent price momentum
        if len(closes) >= 10:
            if closes[-1] > closes[-10]:
                return "LONG"
            if closes[-1] < closes[-10]:
                return "SHORT"

        return "NEUTRAL"

    # ── SMC zone check ────────────────────────────────────────────────────────

    def price_in_smc_zone(self, price: float, bias: str, ctx: SMCContext) -> bool:
        """
        CHANGED: Much wider zone — ATR×1.0 instead of ATR×0.5.
        Also falls back to True if no OBs/FVGs exist (bias is enough).
        """
        half_atr = ctx.current_atr * 1.0
        if half_atr == 0:
            half_atr = price * 0.002

        if bias == "LONG":
            for ob in ctx.bullish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    ob.touches += 1
                    return True
            for fvg in ctx.bullish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
                    return True
            # CHANGED: if no zones exist but bias is LONG, allow entry
            if not ctx.bullish_obs and not ctx.bullish_fvgs:
                return True

        elif bias == "SHORT":
            for ob in ctx.bearish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    ob.touches += 1
                    return True
            for fvg in ctx.bearish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
                    return True
            if not ctx.bearish_obs and not ctx.bearish_fvgs:
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

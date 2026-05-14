"""
smc_analyzer.py – Phase A of SIFM: Higher-Timeframe SMC/ICT Analysis.

Changes vs original:
  • price_in_smc_zone no longer mutates ob.touches.
    The touch counter was causing OBs to expire after 2 uses, silently
    blocking trades on otherwise valid zones.
  • analyse() no longer expires OBs based on touch count; only age matters.
    This keeps signal quality identical across all trades (no zone depletion).
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from candlestick_builder import Candle

logger = logging.getLogger(__name__)

SWING_LOOKBACK = 5


@dataclass
class SwingPoint:
    index:   int
    price:   float
    is_high: bool
    bar_ts:  int

@dataclass
class OrderBlock:
    index:     int
    top:       float
    bottom:    float
    direction: str
    bar_ts:    int
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

    def __init__(self, ob_expiry_bars: int = 20):
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
        structure = self._determine_structure(swings_h, swings_l)

        bull_obs, bear_obs   = self._find_order_blocks(opens, highs, lows, closes, ts)
        bull_fvgs, bear_fvgs = self._find_fvgs(highs, lows, ts)

        # Expire OBs by age only – touch count no longer causes expiry
        n = len(bars)
        for ob in bull_obs + bear_obs:
            if (n - ob.index) > self.ob_expiry_bars:
                ob.expired = True

        # Mark filled FVGs
        last_close = float(closes[-1])
        for fvg in bull_fvgs:
            if last_close <= fvg.bottom:
                fvg.filled = True
        for fvg in bear_fvgs:
            if last_close >= fvg.top:
                fvg.filled = True

        liq_highs = [sh.price for sh in swings_h[-5:]] if swings_h else []
        liq_lows  = [sl.price for sl in swings_l[-5:]] if swings_l else []

        bias = self._determine_bias(structure, last_close, bull_obs, bear_obs,
                                    swings_h, swings_l, atr)

        return SMCContext(
            structure       = structure,
            bias            = bias,
            swing_highs     = swings_h,
            swing_lows      = swings_l,
            bullish_obs     = [ob for ob in bull_obs  if not ob.expired],
            bearish_obs     = [ob for ob in bear_obs  if not ob.expired],
            bullish_fvgs    = [f  for f  in bull_fvgs if not f.filled],
            bearish_fvgs    = [f  for f  in bear_fvgs if not f.filled],
            liquidity_highs = liq_highs,
            liquidity_lows  = liq_lows,
            current_atr     = atr,
        )

    # ── Swing detection ───────────────────────────────────────────────────────

    def _find_swing_highs(self, highs: np.ndarray, ts: List[int]) -> List[SwingPoint]:
        swings, lb = [], SWING_LOOKBACK
        for i in range(lb, len(highs) - lb):
            if highs[i] > np.max(highs[i-lb:i]) and highs[i] > np.max(highs[i+1:i+lb+1]):
                swings.append(SwingPoint(i, float(highs[i]), True, ts[i]))
        return swings

    def _find_swing_lows(self, lows: np.ndarray, ts: List[int]) -> List[SwingPoint]:
        swings, lb = [], SWING_LOOKBACK
        for i in range(lb, len(lows) - lb):
            if lows[i] < np.min(lows[i-lb:i]) and lows[i] < np.min(lows[i+1:i+lb+1]):
                swings.append(SwingPoint(i, float(lows[i]), False, ts[i]))
        return swings

    # ── Market structure ──────────────────────────────────────────────────────

    def _determine_structure(self, swings_h, swings_l) -> str:
        if len(swings_h) < 3 or len(swings_l) < 3:
            return "RANGE"
        last_h = [s.price for s in swings_h[-3:]]
        last_l = [s.price for s in swings_l[-3:]]
        if last_h[-1] > last_h[-2] > last_h[-3] and last_l[-1] > last_l[-2] > last_l[-3]:
            return "UPTREND"
        if last_l[-1] < last_l[-2] < last_l[-3] and last_h[-1] < last_h[-2] < last_h[-3]:
            return "DOWNTREND"
        return "RANGE"

    # ── Order blocks ──────────────────────────────────────────────────────────

    def _find_order_blocks(self, opens, highs, lows, closes,
                           ts) -> Tuple[List[OrderBlock], List[OrderBlock]]:
        bull_obs, bear_obs = [], []
        min_move = float(np.mean(np.abs(np.diff(closes)))) * 3

        for i in range(1, len(closes) - 2):
            if closes[i] < opens[i]:
                if (closes[i+1] > opens[i+1] and closes[i+1] > highs[i] and
                        (closes[i+1] - opens[i+1]) >= min_move):
                    bull_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bullish", bar_ts=ts[i]))
            if closes[i] > opens[i]:
                if (closes[i+1] < opens[i+1] and closes[i+1] < lows[i] and
                        (opens[i+1] - closes[i+1]) >= min_move):
                    bear_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bearish", bar_ts=ts[i]))

        return bull_obs[-10:], bear_obs[-10:]

    # ── Fair Value Gaps ───────────────────────────────────────────────────────

    def _find_fvgs(self, highs, lows, ts) -> Tuple[List[FVG], List[FVG]]:
        bull_fvgs, bear_fvgs = [], []
        for i in range(len(highs) - 2):
            if lows[i+2] > highs[i]:
                bull_fvgs.append(FVG(top=float(lows[i+2]), bottom=float(highs[i]),
                                     direction="bullish", bar_ts=ts[i]))
            if highs[i+2] < lows[i]:
                bear_fvgs.append(FVG(top=float(lows[i]), bottom=float(highs[i+2]),
                                     direction="bearish", bar_ts=ts[i]))
        return bull_fvgs[-10:], bear_fvgs[-10:]

    # ── Bias determination ────────────────────────────────────────────────────

    def _determine_bias(self, structure, price, bull_obs, bear_obs,
                        swings_h, swings_l, atr) -> str:
        if structure == "RANGE":
            return "NEUTRAL"
        if structure == "UPTREND":
            valid_bull = [ob for ob in bull_obs if not ob.expired]
            if valid_bull:
                if price >= valid_bull[-1].bottom - atr * 0.5:
                    return "LONG"
            if swings_l and price > swings_l[-1].price:
                return "LONG"
        if structure == "DOWNTREND":
            valid_bear = [ob for ob in bear_obs if not ob.expired]
            if valid_bear:
                if price <= valid_bear[-1].top + atr * 0.5:
                    return "SHORT"
            if swings_h and price < swings_h[-1].price:
                return "SHORT"
        return "NEUTRAL"

    # ── SMC zone check ────────────────────────────────────────────────────────

    def price_in_smc_zone(self, price: float, bias: str, ctx: SMCContext) -> bool:
        """
        Returns True if price is within ATR/2 of a relevant SMC zone.
        Does NOT mutate ob.touches – zones are never depleted by use.
        """
        half_atr = ctx.current_atr * 0.5
        if half_atr == 0:
            half_atr = price * 0.001

        if bias == "LONG":
            for ob in ctx.bullish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    return True
            for fvg in ctx.bullish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
                    return True
        elif bias == "SHORT":
            for ob in ctx.bearish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    return True
            for fvg in ctx.bearish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
                    return True
        return False

    def get_sl_tp(self, price: float, bias: str,
                  ctx: SMCContext) -> Tuple[float, float]:
        atr = ctx.current_atr if ctx.current_atr else price * 0.002
        min_sl_dist = atr * 1.5

        if bias == "LONG":
            sl = (min(ctx.bullish_obs[-1].bottom - atr * 0.1, price - min_sl_dist)
                  if ctx.bullish_obs else price - min_sl_dist)
            risk = price - sl
            tp   = (ctx.bearish_obs[-1].bottom if ctx.bearish_obs and
                    ctx.bearish_obs[-1].bottom > price + risk
                    else price + risk * 2)
        else:
            sl = (max(ctx.bearish_obs[-1].top + atr * 0.1, price + min_sl_dist)
                  if ctx.bearish_obs else price + min_sl_dist)
            risk = sl - price
            tp   = (ctx.bullish_obs[-1].top if ctx.bullish_obs and
                    ctx.bullish_obs[-1].top < price - risk
                    else price - risk * 2)

        return round(sl, 5), round(tp, 5)

"""
smc_analyzer.py – Phase A of SIFM: Higher-Timeframe SMC/ICT Analysis.

Produces:
  • Swing highs / lows
  • Market structure label (UPTREND / DOWNTREND / RANGE)
  • Bullish / Bearish Order Blocks
  • Fair Value Gaps (FVG)
  • Liquidity pool levels
  • Overall trading bias  (LONG | SHORT | NEUTRAL)
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from candlestick_builder import Candle

logger = logging.getLogger(__name__)

SWING_LOOKBACK = 5   # bars on each side to confirm a swing

# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class SwingPoint:
    index:    int
    price:    float
    is_high:  bool    # True = swing high, False = swing low
    bar_ts:   int     # epoch of that bar

@dataclass
class OrderBlock:
    index:     int
    top:       float   # high[i]
    bottom:    float   # low[i]
    direction: str     # "bullish" | "bearish"
    bar_ts:    int
    touches:   int = 0
    expired:   bool = False

@dataclass
class FVG:
    top:       float
    bottom:    float
    direction: str     # "bullish" | "bearish"
    bar_ts:    int
    filled:    bool = False

@dataclass
class SMCContext:
    structure:      str              # "UPTREND" | "DOWNTREND" | "RANGE"
    bias:           str              # "LONG" | "SHORT" | "NEUTRAL"
    swing_highs:    List[SwingPoint] = field(default_factory=list)
    swing_lows:     List[SwingPoint] = field(default_factory=list)
    bullish_obs:    List[OrderBlock] = field(default_factory=list)
    bearish_obs:    List[OrderBlock] = field(default_factory=list)
    bullish_fvgs:   List[FVG]       = field(default_factory=list)
    bearish_fvgs:   List[FVG]       = field(default_factory=list)
    liquidity_highs: List[float]    = field(default_factory=list)
    liquidity_lows:  List[float]    = field(default_factory=list)
    current_atr:    float           = 0.0


# ─── Main analyzer ────────────────────────────────────────────────────────────

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

        swings_h = self._find_swing_highs(highs, ts)
        swings_l = self._find_swing_lows(lows,   ts)
        structure = self._determine_structure(swings_h, swings_l)

        bull_obs, bear_obs = self._find_order_blocks(opens, highs, lows, closes, ts)
        bull_fvgs, bear_fvgs = self._find_fvgs(highs, lows, ts)

        # Expire OBs touched more than twice, or older than expiry limit
        n = len(bars)
        for ob in bull_obs + bear_obs:
            age = n - ob.index
            if age > self.ob_expiry_bars or ob.touches >= 2:
                ob.expired = True

        # Fill FVGs that price has re-entered
        last_close = float(closes[-1])
        for fvg in bull_fvgs:
            if last_close <= fvg.bottom:   # price filled back into gap
                fvg.filled = True
        for fvg in bear_fvgs:
            if last_close >= fvg.top:
                fvg.filled = True

        # Liquidity pools
        liq_highs = [sh.price for sh in swings_h[-5:]] if swings_h else []
        liq_lows  = [sl.price for sl in swings_l[-5:]] if swings_l else []

        bias = self._determine_bias(structure, last_close, bull_obs, bear_obs,
                                    swings_h, swings_l, atr)

        ctx = SMCContext(
            structure      = structure,
            bias           = bias,
            swing_highs    = swings_h,
            swing_lows     = swings_l,
            bullish_obs    = [ob for ob in bull_obs if not ob.expired],
            bearish_obs    = [ob for ob in bear_obs if not ob.expired],
            bullish_fvgs   = [f for f in bull_fvgs if not f.filled],
            bearish_fvgs   = [f for f in bear_fvgs if not f.filled],
            liquidity_highs = liq_highs,
            liquidity_lows  = liq_lows,
            current_atr    = atr,
        )
        return ctx

    # ── Swing detection ───────────────────────────────────────────────────────

    def _find_swing_highs(self, highs: np.ndarray, ts: List[int]) -> List[SwingPoint]:
        swings = []
        lb = SWING_LOOKBACK
        for i in range(lb, len(highs) - lb):
            left  = highs[i - lb : i]
            right = highs[i + 1 : i + lb + 1]
            if highs[i] > np.max(left) and highs[i] > np.max(right):
                swings.append(SwingPoint(i, float(highs[i]), True, ts[i]))
        return swings

    def _find_swing_lows(self, lows: np.ndarray, ts: List[int]) -> List[SwingPoint]:
        swings = []
        lb = SWING_LOOKBACK
        for i in range(lb, len(lows) - lb):
            left  = lows[i - lb : i]
            right = lows[i + 1 : i + lb + 1]
            if lows[i] < np.min(left) and lows[i] < np.min(right):
                swings.append(SwingPoint(i, float(lows[i]), False, ts[i]))
        return swings

    # ── Market structure ──────────────────────────────────────────────────────

    def _determine_structure(self, swings_h: List[SwingPoint],
                             swings_l: List[SwingPoint]) -> str:
        if len(swings_h) < 3 or len(swings_l) < 3:
            return "RANGE"

        last_h  = [s.price for s in swings_h[-3:]]
        last_l  = [s.price for s in swings_l[-3:]]
        hh_hh   = last_h[-1] > last_h[-2] > last_h[-3]
        hl_hl   = last_l[-1] > last_l[-2] > last_l[-3]
        ll_ll   = last_l[-1] < last_l[-2] < last_l[-3]
        lh_lh   = last_h[-1] < last_h[-2] < last_h[-3]

        if hh_hh and hl_hl:   return "UPTREND"
        if ll_ll and lh_lh:   return "DOWNTREND"
        return "RANGE"

    # ── Order blocks ─────────────────────────────────────────────────────────

    def _find_order_blocks(self, opens, highs, lows, closes,
                           ts) -> Tuple[List[OrderBlock], List[OrderBlock]]:
        bull_obs, bear_obs = [], []
        min_move = float(np.mean(np.abs(np.diff(closes)))) * 3  # strong-move threshold

        for i in range(1, len(closes) - 2):
            # Bullish OB: last bearish candle before a strong upward move
            if closes[i] < opens[i]:                              # candle i is bearish
                if (closes[i+1] > opens[i+1] and                  # i+1 bullish
                        closes[i+1] > highs[i] and                # closes above i's high
                        (closes[i+1] - opens[i+1]) >= min_move):  # strong move
                    bull_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bullish", bar_ts=ts[i]))

            # Bearish OB: last bullish candle before a strong downward move
            if closes[i] > opens[i]:                              # candle i is bullish
                if (closes[i+1] < opens[i+1] and                  # i+1 bearish
                        closes[i+1] < lows[i] and                 # closes below i's low
                        (opens[i+1] - closes[i+1]) >= min_move):  # strong move
                    bear_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bearish", bar_ts=ts[i]))

        # Keep only the most recent 10 of each
        return bull_obs[-10:], bear_obs[-10:]

    # ── Fair Value Gaps ───────────────────────────────────────────────────────

    def _find_fvgs(self, highs, lows, ts) -> Tuple[List[FVG], List[FVG]]:
        bull_fvgs, bear_fvgs = [], []
        for i in range(len(highs) - 2):
            # Bullish FVG: gap between high[i] and low[i+2]
            if lows[i+2] > highs[i]:
                bull_fvgs.append(FVG(
                    top=float(lows[i+2]), bottom=float(highs[i]),
                    direction="bullish", bar_ts=ts[i]))
            # Bearish FVG: gap between low[i] and high[i+2]
            if highs[i+2] < lows[i]:
                bear_fvgs.append(FVG(
                    top=float(lows[i]), bottom=float(highs[i+2]),
                    direction="bearish", bar_ts=ts[i]))
        return bull_fvgs[-10:], bear_fvgs[-10:]

    # ── Bias determination ────────────────────────────────────────────────────

    def _determine_bias(self, structure, price, bull_obs, bear_obs,
                        swings_h, swings_l, atr) -> str:
        if structure == "RANGE":
            return "NEUTRAL"

        if structure == "UPTREND":
            # Price above most recent bullish OB or just swept a liquidity low
            valid_bull = [ob for ob in bull_obs if not ob.expired]
            if valid_bull:
                nearest = valid_bull[-1]
                if price >= nearest.bottom - atr * 0.5:
                    return "LONG"
            # Swept liquidity low (price just went below a swing low then bounced)
            if swings_l:
                last_low = swings_l[-1].price
                if price > last_low:
                    return "LONG"

        if structure == "DOWNTREND":
            valid_bear = [ob for ob in bear_obs if not ob.expired]
            if valid_bear:
                nearest = valid_bear[-1]
                if price <= nearest.top + atr * 0.5:
                    return "SHORT"
            if swings_h:
                last_high = swings_h[-1].price
                if price < last_high:
                    return "SHORT"

        return "NEUTRAL"

    # ── SMC zone check (Phase B.1) ────────────────────────────────────────────

    def price_in_smc_zone(self, price: float, bias: str, ctx: SMCContext) -> bool:
        """Returns True if price is within ATR/2 of a relevant SMC zone."""
        half_atr = ctx.current_atr * 0.5
        if half_atr == 0:
            half_atr = price * 0.001  # 0.1% fallback

        if bias == "LONG":
            for ob in ctx.bullish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    ob.touches += 1
                    return True
            for fvg in ctx.bullish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
                    return True

        elif bias == "SHORT":
            for ob in ctx.bearish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    ob.touches += 1
                    return True
            for fvg in ctx.bearish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
                    return True

        return False

    def get_sl_tp(self, price: float, bias: str,
                  ctx: SMCContext) -> Tuple[float, float]:
        """Calculate stop-loss and take-profit levels from SMC context."""
        atr = ctx.current_atr if ctx.current_atr else price * 0.002
        min_sl_dist = atr * 1.5

        if bias == "LONG":
            # SL below nearest bullish OB or 1.5×ATR
            if ctx.bullish_obs:
                zone_bottom = ctx.bullish_obs[-1].bottom
                sl = min(zone_bottom - atr * 0.1, price - min_sl_dist)
            else:
                sl = price - min_sl_dist

            # TP at nearest bearish OB / FVG above, or 2× risk
            risk = price - sl
            if ctx.bearish_obs:
                tp = ctx.bearish_obs[-1].bottom
                if tp <= price + risk:        # too close
                    tp = price + risk * 2
            else:
                tp = price + risk * 2

        else:  # SHORT
            if ctx.bearish_obs:
                zone_top = ctx.bearish_obs[-1].top
                sl = max(zone_top + atr * 0.1, price + min_sl_dist)
            else:
                sl = price + min_sl_dist

            risk = sl - price
            if ctx.bullish_obs:
                tp = ctx.bullish_obs[-1].top
                if tp >= price - risk:
                    tp = price - risk * 2
            else:
                tp = price - risk * 2

        return round(sl, 5), round(tp, 5)

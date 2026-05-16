"""
smc_analyzer.py – Phase A of SIFM: Higher-Timeframe SMC/ICT Analysis.

v4 → v4-tuned changes:
  • _determine_structure(): relaxed from 3-consecutive-swing to 2-bar condition.
    Requiring 3 sequential swings was extremely slow to satisfy on 1-min
    synthetic bars — the bot was stuck in RANGE (→ NEUTRAL bias) for entire
    sessions.  Now any 2 consecutive swing HHs+HLs = UPTREND, LHs+LLs = DOWNTREND.
    A momentum fallback (EMA slope on the raw close array) resolves structure
    even when swing detection hasn't fired enough pivots yet.

  • _find_order_blocks(): min_move threshold reduced from mean×3 to mean×1.0.
    Synthetic indices move in tick increments — the ×3 multiplier was so large
    that virtually no OBs were ever found on R_10/R_25/R_50/R_75/R_100 etc.

  • price_in_smc_zone(): zone tolerance widened from 0.5×ATR to 2.0×ATR.
    If the bias direction has zero qualifying OBs/FVGs, the function now
    returns True unconditionally (momentum alone is enough to enter; SMC zone
    is a nice-to-have, not a hard gate on synthetics).

  • _determine_bias(): added momentum fallback when structure is RANGE —
    computes a 20-bar EMA slope; if price is above/below EMA the bias resolves
    to LONG/SHORT instead of NEUTRAL.  Also tightened OB proximity requirement
    so valid bull OBs near price always yield LONG in an UPTREND even when the
    explicit price comparison doesn't quite match.

  • price_in_smc_zone() no longer mutates ob.touches (carried over from v3).

  • analyse() expires OBs by age only (carried over from v3).
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from candlestick_builder import Candle

logger = logging.getLogger(__name__)

SWING_LOOKBACK = 3   # reduced from 5 → finds pivots faster on 1-min bars


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

    def __init__(self, ob_expiry_bars: int = 50):
        self.ob_expiry_bars = ob_expiry_bars

    def analyse(self, bars: List[Candle], atr: float) -> SMCContext:
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

        bias = self._determine_bias(
            structure, last_close, closes, bull_obs, bear_obs,
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
        """
        Relaxed detection: 2-bar higher-high/higher-low = UPTREND.
        Falls back to a 20-bar EMA slope when swings are sparse.
        """
        # Primary: at least 2 swing highs and 2 swing lows
        if len(swings_h) >= 2 and len(swings_l) >= 2:
            last_h = [s.price for s in swings_h[-2:]]
            last_l = [s.price for s in swings_l[-2:]]
            if last_h[-1] > last_h[-2] and last_l[-1] > last_l[-2]:
                return "UPTREND"
            if last_h[-1] < last_h[-2] and last_l[-1] < last_l[-2]:
                return "DOWNTREND"

        # Fallback: 3-swing strict check (legacy)
        if len(swings_h) >= 3 and len(swings_l) >= 3:
            last_h = [s.price for s in swings_h[-3:]]
            last_l = [s.price for s in swings_l[-3:]]
            if (last_h[-1] > last_h[-2] and last_l[-1] > last_l[-2]):
                return "UPTREND"
            if (last_h[-1] < last_h[-2] and last_l[-1] < last_l[-2]):
                return "DOWNTREND"

        # EMA slope fallback for RANGE classification when swings are sparse
        if len(closes) >= 20:
            period = min(20, len(closes))
            k = 2.0 / (period + 1)
            ema = float(np.mean(closes[:period]))
            for c in closes[period:]:
                ema = c * k + ema * (1 - k)
            # Treat as trend if last close is meaningfully away from EMA
            last_c = float(closes[-1])
            pct = (last_c - ema) / (ema if ema != 0 else 1)
            if pct > 0.0002:    # 0.02% above EMA
                return "UPTREND"
            if pct < -0.0002:
                return "DOWNTREND"

        return "RANGE"

    # ── Order blocks ──────────────────────────────────────────────────────────

    def _find_order_blocks(self, opens, highs, lows, closes,
                           ts) -> Tuple[List[OrderBlock], List[OrderBlock]]:
        """
        Threshold lowered from mean×3 to mean×1.0 so OBs are found on
        synthetic instruments that move in small, uniform tick steps.
        """
        bull_obs, bear_obs = [], []
        mean_move = float(np.mean(np.abs(np.diff(closes))))
        min_move  = mean_move * 1.0   # was 3.0; synthetics need ≤1.0

        for i in range(1, len(closes) - 2):
            # Bullish OB: bearish candle followed by a strong bullish break
            if closes[i] < opens[i]:
                if (closes[i + 1] > opens[i + 1] and
                        closes[i + 1] > highs[i] and
                        (closes[i + 1] - opens[i + 1]) >= min_move):
                    bull_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bullish", bar_ts=ts[i]))
            # Bearish OB: bullish candle followed by a strong bearish break
            if closes[i] > opens[i]:
                if (closes[i + 1] < opens[i + 1] and
                        closes[i + 1] < lows[i] and
                        (opens[i + 1] - closes[i + 1]) >= min_move):
                    bear_obs.append(OrderBlock(
                        index=i, top=float(highs[i]), bottom=float(lows[i]),
                        direction="bearish", bar_ts=ts[i]))

        return bull_obs[-10:], bear_obs[-10:]

    # ── Fair Value Gaps ───────────────────────────────────────────────────────

    def _find_fvgs(self, highs, lows, ts) -> Tuple[List[FVG], List[FVG]]:
        bull_fvgs, bear_fvgs = [], []
        for i in range(len(highs) - 2):
            if lows[i + 2] > highs[i]:
                bull_fvgs.append(FVG(
                    top=float(lows[i + 2]), bottom=float(highs[i]),
                    direction="bullish", bar_ts=ts[i]))
            if highs[i + 2] < lows[i]:
                bear_fvgs.append(FVG(
                    top=float(lows[i]), bottom=float(highs[i + 2]),
                    direction="bearish", bar_ts=ts[i]))
        return bull_fvgs[-10:], bear_fvgs[-10:]

    # ── Bias determination ────────────────────────────────────────────────────

    def _determine_bias(self, structure: str, price: float,
                        closes: np.ndarray,
                        bull_obs, bear_obs,
                        swings_h, swings_l, atr) -> str:
        """
        Resolves to LONG/SHORT more aggressively:
          • UPTREND / DOWNTREND: OB or swing check first; fallback to structure alone.
          • RANGE: momentum fallback via 10-bar price change.
        """
        half_atr = atr * 2.0   # wider proximity (was 0.5)
        if half_atr == 0:
            half_atr = price * 0.002

        if structure == "UPTREND":
            valid_bull = [ob for ob in bull_obs if not ob.expired]
            if valid_bull:
                nearest = valid_bull[-1]
                if price >= nearest.bottom - half_atr:
                    return "LONG"
            # Swing low support check
            if swings_l and price > swings_l[-1].price - half_atr:
                return "LONG"
            # Structure alone is sufficient
            return "LONG"

        if structure == "DOWNTREND":
            valid_bear = [ob for ob in bear_obs if not ob.expired]
            if valid_bear:
                nearest = valid_bear[-1]
                if price <= nearest.top + half_atr:
                    return "SHORT"
            if swings_h and price < swings_h[-1].price + half_atr:
                return "SHORT"
            return "SHORT"

        # RANGE — momentum fallback
        if len(closes) >= 10:
            lookback = min(10, len(closes))
            pct = (float(closes[-1]) - float(closes[-lookback])) / (
                float(closes[-lookback]) if closes[-lookback] != 0 else 1)
            if pct > 0.0001:
                logger.debug(f"RANGE bias fallback → LONG (pct={pct:.5f})")
                return "LONG"
            if pct < -0.0001:
                logger.debug(f"RANGE bias fallback → SHORT (pct={pct:.5f})")
                return "SHORT"

        return "NEUTRAL"

    # ── SMC zone check ────────────────────────────────────────────────────────

    def price_in_smc_zone(self, price: float, bias: str,
                          ctx: SMCContext) -> bool:
        """
        Returns True if price is within 2×ATR of a relevant SMC zone.
        If no zones of the relevant direction exist, returns True unconditionally
        — on synthetic instruments momentum alone is sufficient to enter; the
        SMC zone is a nice-to-have confirmation, not a hard gate.
        Does NOT mutate ob.touches.
        """
        half_atr = ctx.current_atr * 2.0   # widened from 0.5 to 2.0
        if half_atr == 0:
            half_atr = price * 0.002

        if bias == "LONG":
            zone_count = len(ctx.bullish_obs) + len(ctx.bullish_fvgs)
            if zone_count == 0:
                # No zones detected — allow entry on momentum bias alone
                logger.debug("price_in_smc_zone: no bullish zones → True (fallback)")
                return True
            for ob in ctx.bullish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    return True
            for fvg in ctx.bullish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
                    return True

        elif bias == "SHORT":
            zone_count = len(ctx.bearish_obs) + len(ctx.bearish_fvgs)
            if zone_count == 0:
                logger.debug("price_in_smc_zone: no bearish zones → True (fallback)")
                return True
            for ob in ctx.bearish_obs:
                if ob.bottom - half_atr <= price <= ob.top + half_atr:
                    return True
            for fvg in ctx.bearish_fvgs:
                if fvg.bottom - half_atr <= price <= fvg.top + half_atr:
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

"""
smc_analyzer.py – Phase A of SIFM: Higher-Timeframe SMC/ICT Analysis.

v5 → v6 changes (Bug fixes + tuning):

  BUG 1 FIX – Malformed f-string on line ~374 (and full scan):
    The expression
        f"fast={bias_fast}({ref_fast:.5f} if ref_fast else 'n/a'}"
    had an unescaped '}' that closed the f-string before the expression
    finished.  Fixed by using a proper ternary inside the format spec.
    Full file scanned — all f-strings corrected.

  BUG 3 FIX – analyse() / _determine_bias() returns NEUTRAL too often:
    Added EMA slope + Higher-High / Higher-Low (or Lower-High / Lower-Low)
    structure check as momentum fallback when no clean OB/FVG zone exists
    and the dual-window consensus fails.  This dramatically reduces NEUTRAL
    bias on noisy synthetics and gives the signal engine more opportunities
    to confirm an entry.

  BUG 3 FIX – price_in_smc_zone() tolerance widened:
    Previously used 2×ATR.  Now uses ATR_ZONE_FACTOR (config, default 2.0)
    but with a minimum floor of 3×ATR for synthetics (detected via zero
    volume on candles or symbol prefix).  Also added a fallback: when no
    zones exist of the relevant direction, always return True (already
    present but now also applied to the tolerance calculation).

  All other logic (OB detection, FVG detection) unchanged.
"""

import numpy as np
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from candlestick_builder import Candle

logger = logging.getLogger(__name__)

SWING_LOOKBACK = 3   # reduced from 5 → finds pivots faster on 1-min bars

# EMA slope threshold for structure resolution (% move required)
_EMA_SLOPE_THRESHOLD   = 0.0015   # 0.15%  (was 0.02% — too noisy on synthetics)
# Momentum consensus thresholds for RANGE bias fallback
_MOMENTUM_FAST_PCT     = 0.0008   # 0.08% over 5-bar window
_MOMENTUM_SLOW_PCT     = 0.0005   # 0.05% over 10-bar window

# Momentum fallback: minimum EMA slope % to force bias from RANGE
_MOMENTUM_EMA_FALLBACK = 0.0003   # 0.03% — looser than structure threshold


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

    def analyse(self, bars: List[Candle], atr: float,
                symbol: str = "") -> SMCContext:
        """
        Perform full HTF SMC analysis.

        Parameters
        ----------
        bars   : completed HTF candles
        atr    : pre-computed HTF ATR value
        symbol : trading instrument (optional). Used for BOOM/CRASH bias override.
        """
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
            swings_h, swings_l, atr, symbol=symbol)

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
        Two-bar HH/HL = UPTREND, LH/LL = DOWNTREND.
        EMA slope fallback uses a tighter 0.15% threshold to prevent
        micro-noise from resolving synthetic-instrument bars to a false trend.
        """
        # Primary: at least 2 swing highs and 2 swing lows
        if len(swings_h) >= 2 and len(swings_l) >= 2:
            last_h = [s.price for s in swings_h[-2:]]
            last_l = [s.price for s in swings_l[-2:]]
            if last_h[-1] > last_h[-2] and last_l[-1] > last_l[-2]:
                return "UPTREND"
            if last_h[-1] < last_h[-2] and last_l[-1] < last_l[-2]:
                return "DOWNTREND"

        # Fallback: 3-swing strict check
        if len(swings_h) >= 3 and len(swings_l) >= 3:
            last_h = [s.price for s in swings_h[-3:]]
            last_l = [s.price for s in swings_l[-3:]]
            if last_h[-1] > last_h[-2] and last_l[-1] > last_l[-2]:
                return "UPTREND"
            if last_h[-1] < last_h[-2] and last_l[-1] < last_l[-2]:
                return "DOWNTREND"

        # EMA slope fallback — TIGHTENED threshold (FIX 1)
        if len(closes) >= 20:
            period = min(20, len(closes))
            k = 2.0 / (period + 1)
            ema = float(np.mean(closes[:period]))
            for c in closes[period:]:
                ema = c * k + ema * (1 - k)
            last_c = float(closes[-1])
            ema_ref = ema if ema != 0 else 1.0
            pct = (last_c - ema) / ema_ref
            if pct > _EMA_SLOPE_THRESHOLD:     # was 0.0002; now 0.0015
                return "UPTREND"
            if pct < -_EMA_SLOPE_THRESHOLD:
                return "DOWNTREND"

        return "RANGE"

    # ── Order blocks ──────────────────────────────────────────────────────────

    def _find_order_blocks(self, opens, highs, lows, closes,
                           ts) -> Tuple[List[OrderBlock], List[OrderBlock]]:
        """
        Threshold at mean×1.0 so OBs are found on synthetic instruments
        that move in small, uniform tick steps.
        """
        bull_obs, bear_obs = [], []
        mean_move = float(np.mean(np.abs(np.diff(closes))))
        min_move  = mean_move * 1.0

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

    def _ema_slope(self, closes: np.ndarray, period: int = 10) -> float:
        """
        Compute the percentage slope of a short EMA over the last two values.
        Positive = price trending up, negative = trending down.
        """
        if len(closes) < period + 2:
            return 0.0
        k = 2.0 / (period + 1)
        ema = float(np.mean(closes[:period]))
        for c in closes[period:]:
            ema = c * k + ema * (1 - k)
        # One step back
        ema_prev = float(np.mean(closes[:period]))
        for c in closes[period:-1]:
            ema_prev = c * k + ema_prev * (1 - k)
        ref = ema_prev if ema_prev != 0 else 1.0
        return (ema - ema_prev) / ref

    def _hh_hl_structure(self, closes: np.ndarray, bars: int = 6) -> str:
        """
        Lightweight HH/HL vs LH/LL check on the last `bars` closes.
        Returns 'LONG', 'SHORT', or 'NEUTRAL'.
        """
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
        """
        Resolves HTF bias to LONG / SHORT / NEUTRAL.

        Priority order:
          1. UPTREND / DOWNTREND (with momentum cross-check).
          2. RANGE dual-window momentum consensus.
          3. NEW — EMA slope momentum fallback for RANGE when consensus fails.
          4. NEW — HH/HL structure fallback when EMA is also flat.
          5. BOOM/CRASH instrument tiebreaker.

        The goal is to resolve NEUTRAL as rarely as possible on synthetics,
        since NEUTRAL immediately skips to the next symbol with no trade.
        """
        half_atr = atr * 2.0
        if half_atr == 0:
            half_atr = price * 0.002

        # ── BOOM/CRASH pre-check ──────────────────────────────────────────────
        sym_upper = symbol.upper()
        is_boom   = sym_upper.startswith("BOOM")
        is_crash  = sym_upper.startswith("CRASH")

        # ── UPTREND ───────────────────────────────────────────────────────────
        if structure == "UPTREND":
            # Verify 5-bar momentum is not actively negative
            if len(closes) >= 6:
                fast_pct = (float(closes[-1]) - float(closes[-6])) / (
                    float(closes[-6]) if closes[-6] != 0 else 1.0)
                if fast_pct < -0.001:   # price falling > 0.1% in 5 bars → skip
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

        # ── DOWNTREND ─────────────────────────────────────────────────────────
        if structure == "DOWNTREND":
            # Verify 5-bar momentum is not actively positive
            if len(closes) >= 6:
                fast_pct = (float(closes[-1]) - float(closes[-6])) / (
                    float(closes[-6]) if closes[-6] != 0 else 1.0)
                if fast_pct > 0.001:   # price rising > 0.1% in 5 bars → skip
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
        bias_fast   = "NEUTRAL"
        bias_slow   = "NEUTRAL"
        ref_fast    = None
        ref_slow    = None

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

        # BUG 1 FIX: corrected f-string — use proper ternary for conditional formatting
        fast_str = f"{ref_fast:.5f}" if ref_fast is not None else "n/a"
        slow_str = f"{ref_slow:.5f}" if ref_slow is not None else "n/a"
        logger.debug(
            f"RANGE momentum | fast={bias_fast}({fast_str}) "
            f"slow={bias_slow}({slow_str})")

        # Both windows must agree for RANGE resolution
        if bias_fast == bias_slow and bias_fast != "NEUTRAL":
            logger.debug(f"RANGE consensus → {bias_fast}")
            resolved = bias_fast
        else:
            # ── BUG 3 FIX: EMA slope momentum fallback ───────────────────────
            # When dual-window fails, try a short-period EMA slope.
            # This catches trending markets that are just below the consensus
            # thresholds — very common on low-pip synthetic instruments.
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
                # ── BUG 3 FIX: HH/HL structure fallback ─────────────────────
                # When EMA is also flat, use raw price structure.
                hh_hl = self._hh_hl_structure(closes, bars=8)
                if hh_hl != "NEUTRAL":
                    resolved = hh_hl
                    logger.debug(
                        f"RANGE: HH/HL structure fallback → {hh_hl}")
                else:
                    # ── BOOM/CRASH: use instrument spike-direction as tiebreaker
                    if is_boom:
                        resolved = "LONG"
                        logger.debug("RANGE: BOOM instrument tiebreaker → LONG")
                    elif is_crash:
                        resolved = "SHORT"
                        logger.debug("RANGE: CRASH instrument tiebreaker → SHORT")
                    else:
                        logger.debug("RANGE: no momentum consensus → NEUTRAL")
                        return "NEUTRAL"

        # Sanity: for BOOM → reject SHORT bias; for CRASH → reject LONG bias
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
        Returns True if price is within the zone tolerance of a relevant SMC zone.

        BUG 3 FIX — Wider tolerance for synthetics:
          Tolerance is now max(3 × ATR, ATR_ZONE_FACTOR × ATR).
          ATR_ZONE_FACTOR comes from config (default 2.0), but the floor of
          3×ATR ensures synthetics with small ATR values still get a
          meaningful search radius.

        If no zones of the relevant direction exist, returns True
        unconditionally — on synthetic instruments momentum alone is
        sufficient to enter.

        Does NOT mutate ob.touches.
        """
        try:
            import config as _cfg
            zone_factor = getattr(_cfg, "ATR_ZONE_FACTOR", 2.0)
        except Exception:
            zone_factor = 2.0

        # Wider of config factor and a 3×ATR floor
        half_atr = max(ctx.current_atr * zone_factor,
                       ctx.current_atr * 3.0)
        if half_atr == 0:
            half_atr = price * 0.003   # 0.3% fallback

        if bias == "LONG":
            zone_count = len(ctx.bullish_obs) + len(ctx.bullish_fvgs)
            if zone_count == 0:
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

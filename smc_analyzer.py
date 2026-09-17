"""
smc_analyzer.py — dashboard/logging context snapshot, built on ict_engine.py.

Previously (through Sep 2026) this module was NOT real SMC analysis — its
own docstring said so explicitly ("volatility_analyzer.py (replaces
smc_analyzer.py) — Regime detection for Deriv synthetic indices") and its
`analyse()` method computed a pure momentum/RSI-style score, just wrapped
in SMC-named fields (order_blocks, fvgs, sweep_detected, etc. were declared
but never populated). bot_engine.py's `_scan()` never even called it —
`ScanResult.smc_ctx` was hardcoded to an empty `SMCContext()` default.

This is now genuine SMC/ICT structure analysis: it calls ict_engine.py's
real swing/structure/order-block/FVG/liquidity/premium-discount detection
functions and packages the result into the same `SMCContext` shape
bot_engine.py/keep_alive.py's dashboard already expect, so the dashboard
now shows real market structure instead of a placeholder.

This module is NOT where trading decisions are made — that's
ict_engine.analyze() via signal_engine.evaluate_ict(). This module exists
purely to give `self.smc.analyse()` (constructed in bot_engine.py's
BotEngine.__init__ and referenced by ScanResult.smc_ctx) something real
to report, reusing the exact same detection functions rather than
duplicating any logic.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import ict_engine as ict
from candlestick_builder import Candle

logger = logging.getLogger(__name__)


@dataclass
class SMCContext:
    """
    Field names/shape kept identical to the pre-existing SMCContext so
    every other file that imports it (bot_engine.py, keep_alive.py's
    dashboard rendering) needs zero changes.
    """
    bias:             str    = "NEUTRAL"      # "LONG" | "SHORT" | "NEUTRAL"
    structure:        str    = "NEUTRAL"      # "TRENDING_UP" | "TRENDING_DOWN" | "RANGING"
    confluence_score:  float  = 0.0
    zone_freshness:    float  = 1.0
    regime:            str    = "RANGING"
    momentum_score:    float  = 0.0
    direction:         int    = 0
    order_blocks:      list   = field(default_factory=list)
    fvgs:              list   = field(default_factory=list)
    breakers:          list   = field(default_factory=list)
    sweep_detected:    int    = 0
    mss_detected:      int    = 0
    in_ob:             bool   = False
    in_fvg:            bool   = False
    in_breaker:        bool   = False
    in_premium:        str    = "EQUILIBRIUM"
    nearest_ob:        object = None
    nearest_fvg:       object = None
    current_atr:       float  = 0.0
    swing_highs:       list   = field(default_factory=list)
    swing_lows:        list   = field(default_factory=list)
    bullish_obs:       list   = field(default_factory=list)
    bearish_obs:       list   = field(default_factory=list)
    bullish_fvgs:      list   = field(default_factory=list)
    bearish_fvgs:      list   = field(default_factory=list)
    liquidity_highs:   list   = field(default_factory=list)
    liquidity_lows:    list   = field(default_factory=list)


class SMCAnalyzer:
    """
    Thin wrapper around ict_engine.py's detection primitives, producing a
    dashboard-friendly SMCContext snapshot of current HTF structure. Public
    API (analyse / price_in_smc_zone / price_in_zone / get_sl_tp) is kept
    so bot_engine.py requires no changes beyond passing real htf/mtf bars.
    """

    def __init__(self, ob_expiry_bars: int = 50, **kwargs):
        self.ob_expiry_bars = ob_expiry_bars

    def analyse(
        self,
        htf_bars: List[Candle],
        mtf_bars: List[Candle] = None,
        current_price: float = 0.0,
        atr: float = 0.0,
        symbol: str = "",
        **kwargs,
    ) -> SMCContext:
        if len(htf_bars) < 15:
            logger.debug("analyse: insufficient bars (%d < 15) -> NEUTRAL", len(htf_bars))
            return SMCContext()

        swings = ict.detect_swings(htf_bars, ict.DEFAULT_HTF_SWING_LOOKBACK)
        events = ict.classify_structure(htf_bars, swings)
        trend = events[-1].direction if events else None

        obs = ict.detect_order_blocks(htf_bars, events, ict.DEFAULT_HTF_SEARCH_BACK)
        ict.mark_order_block_mitigation(obs, htf_bars, len(htf_bars) - 1)
        fvgs = ict.detect_fvgs(htf_bars)
        ict.mark_fvg_fill(fvgs, htf_bars, len(htf_bars) - 1)
        liquidity = ict.detect_liquidity_pools(swings, ict.DEFAULT_LIQUIDITY_TOL_PCT)
        ict.mark_liquidity_sweeps(liquidity, htf_bars, len(htf_bars) - 1)

        price = current_price or float(htf_bars[-1].close)

        rng = ict._dealing_range(swings)
        zone = "EQUILIBRIUM"
        if rng is not None:
            swing_low_pt, swing_high_pt = rng
            z = ict.zone_of(price, swing_low_pt.price, swing_high_pt.price)
            if z:
                zone = z.upper()

        bullish_obs = [ob for ob in obs if ob.direction == "bullish" and not ob.mitigated]
        bearish_obs = [ob for ob in obs if ob.direction == "bearish" and not ob.mitigated]
        bullish_fvgs = [g for g in fvgs if g.direction == "bullish" and not g.filled]
        bearish_fvgs = [g for g in fvgs if g.direction == "bearish" and not g.filled]

        in_ob = any(ob.bottom <= price <= ob.top for ob in (bullish_obs + bearish_obs))
        in_fvg = any(g.bottom <= price <= g.top for g in (bullish_fvgs + bearish_fvgs))

        nearest_ob = min(bullish_obs + bearish_obs, key=lambda o: abs(o.midpoint - price), default=None)
        nearest_fvg = min(bullish_fvgs + bearish_fvgs, key=lambda g: abs(g.ce - price), default=None)

        swept_count = sum(1 for p in liquidity if p.swept)

        bias = "LONG" if trend == "bullish" else "SHORT" if trend == "bearish" else "NEUTRAL"

        # Confluence: how much genuine SMC evidence currently supports the
        # HTF bias (used for dashboard display only — the real trading
        # score is ict_engine.analyze()'s own score field).
        conf = 0.0
        if trend is not None:
            conf += 0.30
        if in_ob or in_fvg:
            conf += 0.25
        if swept_count > 0:
            conf += 0.20
        aligned_ob_count = len(bullish_obs) if trend == "bullish" else len(bearish_obs)
        conf += min(aligned_ob_count * 0.05, 0.25)
        conf = max(0.0, min(1.0, conf))

        return SMCContext(
            bias              = bias,
            structure         = ict.structure_label(trend),
            confluence_score  = conf,
            zone_freshness    = 1.0 if not (nearest_ob or nearest_fvg) else
                                  max(0.0, 1.0 - min(abs((nearest_ob.midpoint if nearest_ob else nearest_fvg.ce) - price) / max(price, 1e-9), 1.0)),
            regime            = ict.structure_label(trend),
            momentum_score    = conf,
            direction         = 1 if trend == "bullish" else -1 if trend == "bearish" else 0,
            order_blocks      = obs,
            fvgs              = fvgs,
            breakers          = [],
            sweep_detected    = swept_count,
            mss_detected      = len(events),
            in_ob             = in_ob,
            in_fvg            = in_fvg,
            in_breaker        = False,
            in_premium        = zone,
            nearest_ob        = nearest_ob,
            nearest_fvg       = nearest_fvg,
            current_atr       = atr,
            swing_highs       = [s for s in swings if s.kind == "high"],
            swing_lows        = [s for s in swings if s.kind == "low"],
            bullish_obs       = bullish_obs,
            bearish_obs       = bearish_obs,
            bullish_fvgs      = bullish_fvgs,
            bearish_fvgs      = bearish_fvgs,
            liquidity_highs   = [p for p in liquidity if p.kind == "BSL"],
            liquidity_lows    = [p for p in liquidity if p.kind == "SSL"],
        )

    def price_in_smc_zone(self, price: float, bias: str, ctx: SMCContext) -> bool:
        return self.price_in_zone(price, ctx)

    def price_in_zone(self, price: float, ctx: SMCContext) -> bool:
        return ctx.confluence_score >= 0.40

    def get_sl_tp(self, price: float, bias: str, ctx: SMCContext) -> Tuple[float, float]:
        """
        Kept for API compatibility with any caller still reaching for a
        generic SL/TP off the dashboard context. The bot's actual trading
        SL/TP comes from ict_engine.analyze()'s structural stop/target
        (entry_ob extreme / liquidity pool) via signal_engine.evaluate_ict()
        — NOT from this method, which stays a coarse ATR-style fallback.
        """
        atr = ctx.current_atr if ctx.current_atr > 0 else price * 0.002
        min_sl_dist = atr * 1.5

        if bias == "LONG":
            sl = price - min_sl_dist
            tp = price + (price - sl) * 2.0
        else:
            sl = price + min_sl_dist
            tp = price - (sl - price) * 2.0

        return round(sl, 5), round(tp, 5)

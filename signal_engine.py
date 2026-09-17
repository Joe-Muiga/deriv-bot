"""
signal_engine.py — SMC / ICT signal engine.

COMPLETE REPLACEMENT (Sep 2026 pivot). Every synthetic-index evaluator
that used to live in this file has been DELETED, not disabled/unrouted —
there is no dead code path left that can fire them:

    evaluate_digit, evaluate_digit_parity, evaluate_mean_reversion,
    evaluate_vol_breakout, evaluate_vol_reversion_mult,
    evaluate_popular_indicator (RSI/MACD/Bollinger/ADX/Parabolic SAR/
    Ichimoku), evaluate_vol_regime, evaluate_range_break,
    evaluate_boom_crash, evaluate_drift_fade, evaluate_step,
    evaluate_jump_buildup, evaluate_trend_shift, evaluate_pullback_trend,
    evaluate_fast_mean_reversion, evaluate_spike_catch_1000,
    evaluate_spike_catch_500, evaluate_step_grid

along with every helper that only existed to support them (_tick_quote,
_tick_epoch, _last_digit, _digit_decimals, _chi2_binary,
_digit_hybrid_check) and the pair_suspension.py import (that module was
built for the "which indicator gets picked" popular-indicator pipeline,
which no longer exists — pair_suspension.py itself has been deleted from
the project).

What's left, and why:
  - SignalResult / NONE_RESULT   — the execution-side contract
    (bot_engine.py, deriv_client.py, risk_manager.py, meta_labeling.py all
    consume this shape) is unchanged so nothing downstream needed to be
    rewritten just to keep receiving a signal.
  - compute_enriched_features()  — generic OHLC feature extraction
    (RSI/ROC/Bollinger %B/ATR-expansion/hour-of-day) for meta_labeling.py's
    per-pair EV model. Nothing synthetic-specific about it; it works
    exactly the same on a Forex/gold/commodity candle as on a synthetic
    one, so it's kept as-is.
  - evaluate_ict()                — NEW. The only signal-producing
    function in this file now. Thin wrapper around ict_engine.analyze(),
    which does 100% of the actual SMC/ICT decision-making (market
    structure, order blocks, FVGs, liquidity sweeps, premium/discount,
    killzones — see ict_engine.py's module docstring for the full
    top-down sequence). This function's only job is translating
    ict_engine.ICTSignal into the SignalResult shape everything else
    expects.
  - SignalEngine                  — routing dispatcher, now trivial: every
    symbol in the ICT trading universe goes to evaluate_ict(); everything
    else (any symbol NOT in symbols.ICT_TRADING_UNIVERSE — synthetics,
    crypto, stock indices) gets NONE_RESULT unconditionally. There is no
    strategy selection anymore because there is only one strategy.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

import config
import indicators as ind
import ict_engine as ict
import symbols as sym_module
from candlestick_builder import Candle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type — unchanged shape, kept for compatibility with bot_engine.py /
# deriv_client.py / risk_manager.py / meta_labeling.py.
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    direction: str    # "LONG" | "SHORT" | "NONE"
    strength:  int    # 1-3
    score:     float  # 0.0-1.0 composite probability
    strategy:  str    # which strategy fired — always "ICT_SMC" now
    reason:    str    # human readable

    # Kept for shape-compatibility with deriv_client.py's execution
    # routing. Every ICT signal is a plain directional Multiplier trade —
    # contract_kind is always "RISE_FALL" (bot_engine.py's naming for "a
    # normal LONG/SHORT trade", not literally a Rise/Fall contract) here;
    # digit/match_type are never set since there is no digit-contract
    # strategy left in this codebase.
    contract_kind: str            = "RISE_FALL"
    digit:         Optional[int]  = None
    match_type:    Optional[str]  = None

    # ict_engine.analyze()'s structural stop/target — the entry-OB extreme
    # and the next liquidity pool (or POI-extension fallback), NOT a stake
    # percentage. These pass straight through to deriv_client.buy_multiplier()
    # unmodified — nothing in bot_engine.py rescales or inverts them
    # anymore (the old FLIP_ENTRY / SCALED_SL_TP machinery that used to do
    # that has been deleted along with the synthetic strategies it existed
    # for; see bot_engine.py's module docstring).
    native_stop_price:   Optional[float] = None
    native_target_price: Optional[float] = None
    # The price the signal (and native_stop_price/native_target_price) was
    # actually computed against — threaded through to deriv_client.py's
    # dollar-conversion so it measures distance off the SAME price
    # ict_engine used, rather than re-fetching a fresh quote that may have
    # since moved (see deriv_client.buy_multiplier()'s entry_price param).
    native_entry_price:  Optional[float] = None
    # Always False now — kept only because meta_labeling.py's training-
    # label logic and strategy_stats.get_take_invert_stats() still read
    # this field; nothing sets it True anymore since there's no direction-
    # inversion mechanism left in the codebase.
    execution_inverted:  bool = False

    # ICT-specific context, useful for logging/dashboard — NOT read by
    # bot_engine.py's execution path, safe to ignore if you don't need it.
    ict_stage:      str = "NO_DATA"
    ict_rr_ratio:   float = 0.0
    ict_killzone:   Optional[str] = None


NONE_RESULT = SignalResult("NONE", 0, 0.0, "NONE", "No signal")


# ---------------------------------------------------------------------------
# Helpers — candle arrays
# ---------------------------------------------------------------------------

def _arrays(bars: List[Candle]):
    C = np.array([b.close for b in bars], dtype=float)
    H = np.array([b.high  for b in bars], dtype=float)
    L = np.array([b.low   for b in bars], dtype=float)
    return C, H, L


def _last(arr: np.ndarray, back: int = 1) -> float:
    valid = arr[~np.isnan(arr)]
    if len(valid) < back:
        return float("nan")
    return float(valid[-back])


# ---------------------------------------------------------------------------
# Enriched features for meta_labeling.py's per-pair EV model — generic OHLC
# feature extraction, unrelated to which strategy produced the signal.
# ---------------------------------------------------------------------------

def compute_enriched_features(ltf_bars: List[Candle], timestamp: Optional[float] = None) -> Dict[str, float]:
    """
    Returns {"rsi", "roc", "bb_pct_b", "atr_expansion_ratio", "hour_utc"} —
    exactly meta_labeling.ENRICHED_FEATURE_KEYS — or {} if there isn't
    enough bar history yet (caller should log/pass through an empty dict
    in that case, not fabricate values; meta_labeling.py already treats a
    missing/empty enriched dict as "fall back to the global model").

    rsi                  : RSI(14), last closed bar
    roc                  : ROC(10), last closed bar
    bb_pct_b             : Bollinger %B = (close - lower) / (upper - lower),
                            BB(20, 2.0) — 0.0 at the lower band, 1.0 at the
                            upper band, can exceed [0,1] on a strong move
    atr_expansion_ratio   : ATR(14) now / ATR(14) 10 bars ago — >1 means
                            volatility is expanding, <1 means contracting
    hour_utc              : UTC hour (0-23) of `timestamp` (defaults to now)
    """
    if len(ltf_bars) < 30:
        return {}

    C, H, L = _arrays(ltf_bars)
    rsi_arr = ind.rsi(C, 14)
    roc_arr = ind.roc(C, 10)
    upper, mid, lower = ind.bollinger_bands(C, 20, 2.0)
    atr_arr = ind.atr(H, L, C, config.ATR_PERIOD)

    last_rsi   = _last(rsi_arr)
    last_roc   = _last(roc_arr)
    last_upper = _last(upper)
    last_lower = _last(lower)
    atr_now    = _last(atr_arr, 1)
    atr_prior  = _last(atr_arr, 10)

    band_width = last_upper - last_lower
    bb_pct_b = (
        (float(C[-1]) - last_lower) / band_width
        if band_width > 0 and not math.isnan(band_width) else float("nan")
    )
    atr_expansion_ratio = (
        atr_now / atr_prior if atr_prior and not math.isnan(atr_prior) and atr_prior > 0
        else float("nan")
    )

    ts = timestamp if timestamp is not None else datetime.now(timezone.utc).timestamp()
    hour_utc = datetime.fromtimestamp(ts, tz=timezone.utc).hour

    feat = {
        "rsi": last_rsi,
        "roc": last_roc,
        "bb_pct_b": bb_pct_b,
        "atr_expansion_ratio": atr_expansion_ratio,
        "hour_utc": float(hour_utc),
    }
    # Drop any NaN entries rather than shipping them into a JSON column —
    # _PairEVModel._numeric_subset() only keeps int/float, and a NaN would
    # silently poison DictVectorizer/LogisticRegression's training set.
    return {k: v for k, v in feat.items() if not (isinstance(v, float) and math.isnan(v))}


# ---------------------------------------------------------------------------
# The one and only strategy: SMC / ICT
# ---------------------------------------------------------------------------

def evaluate_ict(
    htf_bars: List[Candle],
    mtf_bars: List[Candle],
    ltf_bars: List[Candle],
    symbol: str,
) -> SignalResult:
    """
    Runs ict_engine.analyze() and translates its ICTSignal into a
    SignalResult. All the actual decision-making (bias, POI, sweep,
    CHoCH/BOS, entry OB, stop, target, killzone, min R:R) happens inside
    ict_engine.analyze() — see that module's docstring for the full
    sequence. This function only:
      1. pulls the per-asset-class tuning knobs out of config.py
      2. calls analyze()
      3. maps direction=="NONE" -> NONE_RESULT (with the stage/reason kept
         for logging so a REJECTED log line says WHY, same as every other
         evaluator in this codebase always has)
      4. maps a real signal's score into the 1-3 strength scale
         SignalEngine.evaluate() gates on
    """
    asset_class = sym_module.get_ict_asset_class(symbol)
    tol_map = getattr(config, "ICT_LIQUIDITY_TOLERANCE_PCT", {})
    tolerance = tol_map.get(asset_class, tol_map.get("default", 0.0008))

    result = ict.analyze(
        symbol, htf_bars, mtf_bars, ltf_bars,
        htf_swing_lookback=getattr(config, "ICT_HTF_SWING_LOOKBACK", 3),
        mtf_swing_lookback=getattr(config, "ICT_MTF_SWING_LOOKBACK", 2),
        ltf_swing_lookback=getattr(config, "ICT_LTF_SWING_LOOKBACK", 2),
        min_rr=getattr(config, "ICT_MIN_RR_RATIO", 2.0),
        liquidity_tolerance_pct=tolerance,
        require_killzone=getattr(config, "ICT_KILLZONE_HARD_FILTER", True),
    )

    if result.direction == "NONE":
        logger.debug(
            f"REJECTED: {symbol} ICT_SMC stage={result.stage} — {result.reason}"
        )
        return SignalResult(
            direction="NONE", strength=0, score=0.0, strategy="ICT_SMC",
            reason=result.reason, ict_stage=result.stage,
            ict_rr_ratio=result.rr_ratio, ict_killzone=result.killzone,
        )

    if result.score >= 0.75:
        strength = 3
    elif result.score >= 0.55:
        strength = 2
    else:
        strength = 1

    return SignalResult(
        direction=result.direction,
        strength=strength,
        score=result.score,
        strategy="ICT_SMC",
        reason=result.reason,
        native_entry_price=result.entry,
        native_stop_price=result.stop,
        native_target_price=result.target,
        ict_stage=result.stage,
        ict_rr_ratio=result.rr_ratio,
        ict_killzone=result.killzone,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class SignalEngine:

    def __init__(self, *args, **kwargs):
        pass

    def evaluate(
        self,
        ltf_bars: List[Candle],
        symbol: str,
        htf_bars: Optional[List[Candle]] = None,
        mtf_bars: Optional[List[Candle]] = None,
        **kwargs,
    ) -> SignalResult:
        """
        Every symbol in symbols.ICT_TRADING_UNIVERSE is routed to
        evaluate_ict(); everything else gets NONE_RESULT unconditionally
        — there is no other strategy left to fall back to. htf_bars/
        mtf_bars are required for a real evaluation (ICT is inherently
        multi-timeframe — see ict_engine.py); if bot_engine.py's caller
        doesn't have them yet (still warming up), this returns NONE_RESULT
        rather than guessing.
        """
        if symbol not in sym_module.ICT_TRADING_UNIVERSE:
            logger.debug(f"REJECTED: {symbol} not in ICT trading universe")
            return NONE_RESULT

        if not htf_bars or not mtf_bars:
            logger.debug(f"REJECTED: {symbol} missing htf/mtf bars for ICT evaluation")
            return NONE_RESULT

        result = evaluate_ict(htf_bars, mtf_bars, ltf_bars, symbol)

        if result.strength >= 2:
            logger.info(
                f"SIGNAL: {symbol} {result.direction} {result.strategy} "
                f"strength={result.strength} score={result.score:.3f} "
                f"rr={result.ict_rr_ratio:.2f} killzone={result.ict_killzone or 'none'}"
            )
            return result

        logger.info(
            f"REJECTED: {symbol} {result.strategy} strength={result.strength} "
            f"score={result.score:.3f} stage={result.ict_stage} — below threshold"
        )
        # Return `result` itself (direction forced to NONE) rather than the
        # shared NONE_RESULT constant — result already carries the REAL
        # ict_stage/reason/rr_ratio/killzone from ict_engine.analyze() (e.g.
        # "AWAITING_POI_ARRIVAL — watching this order block"), which is far
        # more useful for logging/debugging than NONE_RESULT's generic
        # "No signal" text. bot_engine._scan() only checks
        # `sig.direction == "NONE"` to decide whether to act on it, so
        # forcing direction here is all that's needed to keep it a no-op.
        result.direction = "NONE"
        return result

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

ADDED BACK (stpRNG handoff): evaluate_step_grid() and its flip-entry
transform, _apply_flip_and_swap_levels(), are reintroduced as new,
standalone code for exactly one symbol — stpRNG (config.STEP_GRID_SYMBOLS)
— running fully in parallel to, and independent of, evaluate_ict(). See
evaluate_step_grid_final() below for the full stpRNG pipeline (raw AND-gate
evaluation -> flip transform -> no-consecutive-same-direction gate) and
SignalEngine.evaluate()'s stpRNG-specific branch, checked ahead of the ICT
universe check. Nothing here changes evaluate_ict()/ict_engine.py or how
the 11 ICT symbols are evaluated.

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

import json
import logging
import math
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

import config
import indicators as ind
import ict_engine as ict
import strategy_stats
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
# stpRNG — Step Grid (independent of evaluate_ict() / ict_engine.py).
#
# Ported verbatim from the synthetic-indices bot per the handoff — the
# AND-gate conditions, indicator periods, and stop/target construction
# below are NOT redesigned, simplified, or reinterpreted in any way.
# ---------------------------------------------------------------------------

def evaluate_step_grid(
    ltf_bars: List["Candle"], symbol: str, current_price: Optional[float] = None
) -> SignalResult:
    """
    stpRNG — Indicator-grid entry, hard AND-gate. ALL four must
    agree before firing (long: EMA10>EMA20, price>EMA20, RSI>55, MACD
    line>signal line; short: every condition mirrored) — this is a gate,
    not a scored/weighted pick, so any single condition failing rejects
    the whole signal. Stop sits outside the recent range that defined the
    setup (the greater of a recent swing extreme and EMA20 itself, plus a
    buffer), not an arbitrary ATR multiple alone; target is a fixed
    reward:risk multiple of that stop distance.

    current_price (handoff point 4, Sep 2026), optional: a live tick price
    to use as "the current price" in place of the last COMPLETED bar's
    close. ltf_bars only gains a new element once a full LTF-granularity
    window closes (5 min for stpRNG), so C[-1] alone can be stale by up to
    that whole window at the moment this runs. When provided, this
    overrides last_close for the AND-gate's price-vs-EMA20 check and for
    native_entry_price — EMA/RSI/MACD still compute from the completed-bar
    series C unchanged (indicator lag over a real window is expected and
    not what this addresses).
    """
    min_bars = getattr(config, "STEP_GRID_MIN_BARS", 30)
    if len(ltf_bars) < min_bars:
        return NONE_RESULT

    C, H, L = _arrays(ltf_bars)
    if len(C) < 2 or math.isnan(float(C[-1])):
        return NONE_RESULT

    ema10 = ind.ema(C, getattr(config, "STEP_GRID_EMA_FAST_PERIOD", 10))
    ema20 = ind.ema(C, getattr(config, "STEP_GRID_EMA_SLOW_PERIOD", 20))
    rsi_vals = ind.rsi(C, getattr(config, "STEP_GRID_RSI_PERIOD", 14))
    macd_line, macd_signal, _ = ind.macd(
        C, getattr(config, "STEP_GRID_MACD_FAST", 12),
        getattr(config, "STEP_GRID_MACD_SLOW", 26),
        getattr(config, "STEP_GRID_MACD_SIGNAL", 9))
    atr_val = _last(ind.atr(H, L, C, 14)) or 1e-9

    last_close = float(C[-1])
    if current_price is not None and not math.isnan(current_price):
        last_close = float(current_price)
    e10, e20 = float(ema10[-1]), float(ema20[-1])
    last_rsi = float(rsi_vals[-1])
    m_line, m_sig = float(macd_line[-1]), float(macd_signal[-1])

    rsi_long_min  = getattr(config, "STEP_GRID_RSI_LONG_MIN", 55.0)
    rsi_short_max = getattr(config, "STEP_GRID_RSI_SHORT_MAX", 45.0)

    long_gate = (e10 > e20) and (last_close > e20) and (last_rsi > rsi_long_min) and (m_line > m_sig)
    short_gate = (e10 < e20) and (last_close < e20) and (last_rsi < rsi_short_max) and (m_line < m_sig)

    if long_gate == short_gate:  # neither fired, or (impossible) both did
        logger.debug(f"REJECTED: {symbol} STEP_GRID strength=0 score=0.000 — below threshold")
        return NONE_RESULT

    range_lookback = getattr(config, "STEP_GRID_RANGE_LOOKBACK", 20)
    stop_buf_mult  = getattr(config, "STEP_GRID_STOP_BUFFER_ATR_MULT", 0.30)
    rr_ratio       = getattr(config, "STEP_GRID_RR_RATIO", 2.0)

    if long_gate:
        direction = "LONG"
        range_low = float(np.min(L[-range_lookback:]))
        native_entry_price = last_close
        native_stop_price = min(range_low, e20) - stop_buf_mult * atr_val
        risk = native_entry_price - native_stop_price
        if risk <= 0:
            return NONE_RESULT
        native_target_price = native_entry_price + rr_ratio * risk
    else:
        direction = "SHORT"
        range_high = float(np.max(H[-range_lookback:]))
        native_entry_price = last_close
        native_stop_price = max(range_high, e20) + stop_buf_mult * atr_val
        risk = native_stop_price - native_entry_price
        if risk <= 0:
            return NONE_RESULT
        native_target_price = native_entry_price - rr_ratio * risk

    score = 0.75  # hard AND-gate — either every condition agrees (fixed high confidence) or it doesn't fire
    strength = 3

    logger.info(
        f"SIGNAL: {symbol} {direction} STEP_GRID strength={strength} score={score:.3f} "
        f"entry={native_entry_price:.5f} stop={native_stop_price:.5f} target={native_target_price:.5f}"
    )
    return SignalResult(
        direction=direction, strength=strength, score=score,
        strategy="STEP_GRID",
        reason=(
            f"AND-gate: EMA10/20={e10:.5f}/{e20:.5f}, price_vs_EMA20, "
            f"RSI={last_rsi:.1f}, MACD={m_line:.5f} vs {m_sig:.5f}"
        ),
        native_entry_price=native_entry_price,
        native_stop_price=native_stop_price,
        native_target_price=native_target_price,
    )


def _apply_flip_and_swap_levels(sig: SignalResult) -> Optional[SignalResult]:
    """
    For stpRNG only. Transforms a firing signal for IMMEDIATE execution:

      1. Direction is FLIPPED (LONG -> SHORT, SHORT -> LONG). Intentional:
         the evaluator's own native_stop_price always sits on the side of
         entry OPPOSITE its native_target_price, so using native_stop_price
         as a take-profit (step 3) only makes geometric sense for the
         OPPOSITE direction from what the evaluator signalled.
      2. Entry = native_entry_price, used directly, unchanged.
      3. Take-profit = the evaluator's original native_stop_price,
         unchanged (native_target_price is no longer used at all).
      4. Stop-loss is computed fresh, on the side of entry OPPOSITE the
         new take-profit, so that take_profit_distance / stop_loss_distance
         is STRICTLY greater than FLIP_ENTRY_MIN_RR_RATIO.

    Returns None (caller drops the signal for this cycle) if the
    resulting take-profit distance isn't strictly positive.
    """
    if sig.direction not in ("LONG", "SHORT"):
        return None
    if sig.native_entry_price is None or sig.native_stop_price is None:
        return None

    flipped_direction = "SHORT" if sig.direction == "LONG" else "LONG"
    entry  = float(sig.native_entry_price)
    target = float(sig.native_stop_price)   # evaluator's old stop -> new take-profit
    is_long = flipped_direction == "LONG"

    raw_target_distance = (target - entry) if is_long else (entry - target)
    if raw_target_distance <= 0:
        logger.warning(
            f"FLIP-ENTRY DROPPED: {sig.strategy} {sig.direction} — "
            f"non-positive take-profit distance after flip to "
            f"{flipped_direction} (entry={entry:.5f}, take-profit="
            f"{target:.5f}), refusing to trade"
        )
        return None

    # Handoff point 1 (Sep 2026): scale both the take-profit distance and
    # the stop-loss distance from entry down by STEP_GRID_SL_TP_DISTANCE_MULT
    # (config.py — currently 0.125, i.e. 1/8 of the original raw swing
    # distance), so the take-profit sits somewhere realistically reachable.
    # Applied here, to target_distance, BEFORE the stop-loss is derived
    # from it below — sl_distance is already a pure function of
    # target_distance (via ratio/margin, both fixed constants), so scaling
    # target_distance first automatically scales sl_distance by the same
    # factor and the resulting R:R ratio (target_distance / sl_distance =
    # ratio / (1 - margin)) is exactly unchanged regardless of this
    # multiplier's value. stpRNG-only — STEP_GRID_SL_TP_DISTANCE_MULT is
    # never read by evaluate_ict() or any other symbol's code path.
    distance_mult   = getattr(config, "STEP_GRID_SL_TP_DISTANCE_MULT", 0.125)
    target_distance = raw_target_distance * distance_mult
    target          = (entry + target_distance) if is_long else (entry - target_distance)

    ratio  = getattr(config, "FLIP_ENTRY_MIN_RR_RATIO", 2.0)
    margin = getattr(config, "FLIP_ENTRY_SL_SAFETY_MARGIN", 0.10)
    max_sl_distance = target_distance / ratio
    sl_distance     = max_sl_distance * (1.0 - margin)
    new_stop        = (entry - sl_distance) if is_long else (entry + sl_distance)

    logger.info(
        f"FLIP-ENTRY: {sig.strategy} {sig.direction} -> {flipped_direction} | "
        f"entry={entry:.5f} (native, immediate) | take-profit={target:.5f} "
        f"(was native_stop, distance x{distance_mult}) | new stop-loss="
        f"{new_stop:.5f} | ratio={(target_distance / sl_distance):.2f}:1 "
        f"(> {ratio:.1f} required)"
    )
    return replace(
        sig,
        direction=flipped_direction,
        native_entry_price=entry,
        native_stop_price=new_stop,
        native_target_price=target,
        execution_inverted=True,
    )


def _apply_distance_scaling(sig: SignalResult) -> Optional[SignalResult]:
    """
    For stpRNG only. Used in place of _apply_flip_and_swap_levels() when
    STEP_GRID_INVERT_SIGNAL_ENABLED is False (config.py) — i.e. the raw
    AND-gate signal is executed AS-IS, direction unchanged, instead of
    flipped. Keeps evaluate_step_grid()'s own direction/entry/stop/target
    exactly as computed, EXCEPT it scales both the stop-loss distance and
    the take-profit distance down by STEP_GRID_SL_TP_DISTANCE_MULT — the
    same scaling _apply_flip_and_swap_levels() applies on the flipped
    path, so TP stays realistically reachable either way. Scaling both
    distances by the same factor leaves the R:R ratio (target_distance /
    stop_distance = STEP_GRID_RR_RATIO, already baked into
    evaluate_step_grid()'s own target = entry +/- rr_ratio*risk)
    unchanged.

    Returns None if the resulting stop/target distance isn't strictly
    positive (should not happen — evaluate_step_grid() already checked
    this before scaling — but never trust a scaled value without
    re-checking).
    """
    if sig.direction not in ("LONG", "SHORT"):
        return None
    if sig.native_entry_price is None or sig.native_stop_price is None or sig.native_target_price is None:
        return None

    entry  = float(sig.native_entry_price)
    stop   = float(sig.native_stop_price)
    target = float(sig.native_target_price)
    is_long = sig.direction == "LONG"

    raw_stop_distance   = (entry - stop)   if is_long else (stop - entry)
    raw_target_distance = (target - entry) if is_long else (entry - target)
    if raw_stop_distance <= 0 or raw_target_distance <= 0:
        logger.warning(
            f"RAW-ENTRY DROPPED: {sig.strategy} {sig.direction} — "
            f"non-positive stop/target distance (entry={entry:.5f}, "
            f"stop={stop:.5f}, target={target:.5f}), refusing to trade"
        )
        return None

    distance_mult = getattr(config, "STEP_GRID_SL_TP_DISTANCE_MULT", 0.125)
    new_stop_distance   = raw_stop_distance   * distance_mult
    new_target_distance = raw_target_distance * distance_mult
    new_stop   = (entry - new_stop_distance)   if is_long else (entry + new_stop_distance)
    new_target = (entry + new_target_distance) if is_long else (entry - new_target_distance)

    logger.info(
        f"RAW-ENTRY: {sig.strategy} {sig.direction} (inversion disabled) | "
        f"entry={entry:.5f} | stop-loss={new_stop:.5f} (was {stop:.5f}) | "
        f"take-profit={new_target:.5f} (was {target:.5f}) | distance x"
        f"{distance_mult} | ratio={(new_target_distance / new_stop_distance):.2f}:1"
    )
    return replace(
        sig,
        native_entry_price=entry,
        native_stop_price=new_stop,
        native_target_price=new_target,
        execution_inverted=False,
    )


# ---------------------------------------------------------------------------
# stpRNG direction-alternation state — persisted to disk (handoff point 2,
# Sep 2026) so the "no consecutive same-direction trade" gate below
# survives the bot's rolling ~11-minute redeploy cycle instead of
# forgetting the last executed direction (and so potentially allowing two
# same-direction trades back to back) every time the process restarts.
#
# Reuses strategy_stats.py's own DATA_DIR (the STRATEGY_STATS_DIR env var,
# defaulting to this file's own directory) — "whatever the codebase
# already uses for cross-restart state" — under a clearly namespaced key
# (step_index_last_direction) and its own file, so it can't collide with
# or be confused for strategy_stats.py's own data.
#
# CAVEAT, documented here the same way strategy_stats.py/trade_journal.py
# already document it for their own storage: Render's FREE-TIER
# filesystem is ephemeral across an actual redeploy. This file survives
# an in-place crash/restart within the same container, but NOT a real
# free-tier redeploy unless a persistent disk is mounted and
# STRATEGY_STATS_DIR points at its mount path. Without that, this still
# improves on the old pure in-memory state (which forgot on every single
# restart, guaranteed) but won't be bulletproof across every redeploy —
# mount a disk for that.
# ---------------------------------------------------------------------------
_STEP_STATE_DIR  = getattr(strategy_stats, "DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
_STEP_STATE_PATH = os.path.join(_STEP_STATE_DIR, "step_index_direction_state.json")
_STEP_STATE_KEY  = "step_index_last_direction"


def _load_step_direction_state() -> Optional[str]:
    try:
        with open(_STEP_STATE_PATH, "r") as f:
            data = json.load(f)
        direction = data.get(_STEP_STATE_KEY)
        if direction in ("LONG", "SHORT"):
            return direction
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(
            f"STEP_GRID: failed to read persisted direction state "
            f"({_STEP_STATE_PATH}): {exc} — starting with no known last "
            f"direction (safe default: the alternation gate simply won't "
            f"reject anything until the next stpRNG trade executes)"
        )
    return None


def _save_step_direction_state(direction: str) -> None:
    try:
        os.makedirs(_STEP_STATE_DIR, exist_ok=True)
        tmp_path = _STEP_STATE_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(
                {
                    _STEP_STATE_KEY: direction,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                f,
            )
        os.replace(tmp_path, _STEP_STATE_PATH)  # atomic on POSIX
    except Exception as exc:
        logger.warning(
            f"STEP_GRID: failed to persist direction state "
            f"({_STEP_STATE_PATH}): {exc} — alternation gate will forget "
            f"this particular update on the next restart"
        )


class _StepGridDirectionState:
    """
    stpRNG-only state: the direction of the last stpRNG trade that was
    actually confirmed placed at the broker (i.e. the real, post-flip
    direction — see _apply_flip_and_swap_levels()). Lives here, not in
    bot_engine.py's shared dicts, and is never read by evaluate_ict() or
    any ICT-symbol code path. Persisted to disk — see the block above.
    """

    def __init__(self):
        self.last_executed_direction: Optional[str] = _load_step_direction_state()
        if self.last_executed_direction:
            logger.info(
                f"STEP_GRID: restored last executed direction from "
                f"persisted state: {self.last_executed_direction}"
            )

    def record_executed(self, direction: str) -> None:
        self.last_executed_direction = direction
        _save_step_direction_state(direction)


_step_grid_direction_state = _StepGridDirectionState()


def evaluate_step_grid_final(
    ltf_bars: List["Candle"], symbol: str, current_price: Optional[float] = None
) -> SignalResult:
    """
    stpRNG's full, independent pipeline:
      1. evaluate_step_grid() — raw AND-gate evaluation.
      2. STEP_GRID_INVERT_SIGNAL_ENABLED (config.py) selects the
         execution transform: _apply_flip_and_swap_levels() (flips
         direction, reuses the raw stop as take-profit) when True, or
         _apply_distance_scaling() (keeps the raw direction/stop/target
         as-is, only scales distances) when False — currently False, so
         the raw signal executes unflipped.
      3. "No consecutive same-direction trade" gate — reject (not queue)
         a same-direction signal until a stpRNG trade actually executes
         in the opposite direction. Compared against the real, final
         (post-transform) direction — see _StepGridDirectionState above.

    current_price (handoff point 4, Sep 2026): the live in-progress tick
    price, when the caller has one available (bot_engine._scan() threads
    it through from CandlestickBuilder.current_price). Passed straight to
    evaluate_step_grid() — see its docstring for exactly what it affects.
    Optional; None falls back to the old completed-bar-close behavior.

    Returns the SignalResult exactly as it should be executed (direction/
    native_entry_price/native_stop_price/native_target_price already the
    final, broker-bound values) or NONE_RESULT. Entirely independent of
    evaluate_ict() / ict_engine.py / htf_bars / mtf_bars / killzones /
    order blocks — this function only ever reads ltf_bars.
    """
    raw = evaluate_step_grid(ltf_bars, symbol, current_price=current_price)
    if raw.direction not in ("LONG", "SHORT"):
        return NONE_RESULT

    # Handoff follow-up (Sep 2026): inversion disabled — the user
    # confirmed the flip should stop and the raw AND-gate signal should
    # execute as-is. Gated on config.STEP_GRID_INVERT_SIGNAL_ENABLED
    # (default False) rather than deleted, so it's a one-line revert if
    # ever wanted back. Either path still applies the same
    # STEP_GRID_SL_TP_DISTANCE_MULT distance scaling.
    if getattr(config, "STEP_GRID_INVERT_SIGNAL_ENABLED", False):
        candidate = _apply_flip_and_swap_levels(raw)
    else:
        candidate = _apply_distance_scaling(raw)
    if candidate is None:
        return NONE_RESULT

    if candidate.direction == _step_grid_direction_state.last_executed_direction:
        logger.debug(
            f"REJECTED: {symbol} STEP_GRID {candidate.direction} — same "
            f"direction as last executed stpRNG trade, holding until an "
            f"opposite-direction signal fires"
        )
        return NONE_RESULT

    return candidate


def record_step_grid_execution(direction: str) -> None:
    """
    Call ONLY after a stpRNG trade is confirmed placed at the broker
    (i.e. bot_engine._execute() has a non-None buy_resp), passing the
    real direction that was sent (the flipped direction already on the
    executed SignalResult). Updates the stpRNG-only "last executed
    direction" state the no-consecutive-same-direction gate above reads.
    Firing a signal alone does NOT call this — only a confirmed trade.
    """
    _step_grid_direction_state.record_executed(direction)


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
        current_price: Optional[float] = None,
        **kwargs,
    ) -> SignalResult:
        """
        stpRNG (config.STEP_GRID_SYMBOLS) is checked FIRST, ahead of the
        ICT-universe check below, and routed to its own independent
        evaluate_step_grid_final() — a separate dispatch branch that
        never touches evaluate_ict()/ict_engine.py and, being LTF-only,
        does not require htf_bars/mtf_bars.

        current_price (handoff point 4, Sep 2026): forwarded only to the
        stpRNG branch below — evaluate_ict() never receives it and its
        signature/behavior is untouched.

        Every symbol in symbols.ICT_TRADING_UNIVERSE is routed to
        evaluate_ict(); everything else gets NONE_RESULT unconditionally
        — there is no other strategy left to fall back to. htf_bars/
        mtf_bars are required for a real evaluation (ICT is inherently
        multi-timeframe — see ict_engine.py); if bot_engine.py's caller
        doesn't have them yet (still warming up), this returns NONE_RESULT
        rather than guessing.
        """
        if symbol in getattr(config, "STEP_GRID_SYMBOLS", ()):
            if not ltf_bars:
                logger.debug(f"REJECTED: {symbol} missing ltf bars for STEP_GRID evaluation")
                return NONE_RESULT
            result = evaluate_step_grid_final(ltf_bars, symbol, current_price=current_price)
            if result.direction != "NONE":
                logger.info(
                    f"SIGNAL: {symbol} {result.direction} {result.strategy} "
                    f"strength={result.strength} score={result.score:.3f}"
                )
            return result

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

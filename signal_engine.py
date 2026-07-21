"""
Multi-strategy signal engine.
Each symbol is routed to exactly ONE strategy evaluator based on which
config symbol-list it belongs to. No cross-strategy voting, no SMC,
no order blocks, no HTF bias — each category gets its own independent
purpose-built evaluator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

import config
import indicators as ind
from candlestick_builder import Candle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    direction: str    # "LONG" | "SHORT" | "NONE"
    strength:  int    # 1-3
    score:     float  # 0.0-1.0 composite probability
    strategy:  str    # which strategy fired
    reason:    str    # human readable


NONE_RESULT = SignalResult("NONE", 0, 0.0, "NONE", "No signal")


# ---------------------------------------------------------------------------
# Helpers
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
# Strategy 1 — Digit Over/Under
# ---------------------------------------------------------------------------

def evaluate_digit(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    if len(ltf_bars) < 25:
        return NONE_RESULT

    C, H, L = _arrays(ltf_bars)
    rsi = ind.rsi(C, 14)
    upper, mid, lower = ind.bollinger_bands(C, 20, 2.0)
    roc = ind.roc(C, 10)

    raw_score, digit_dir = ind.digit_score(
        close=C, rsi=rsi, bb_upper=upper, bb_mid=mid, bb_lower=lower, roc=roc
    )
    partial_score = raw_score / 8.0

    if digit_dir is None or raw_score < 6:
        logger.debug(f"REJECTED: {symbol} DIGIT strength=0 score={partial_score:.3f} — below threshold")
        return SignalResult("NONE", 0, partial_score, "DIGIT", "Below entry threshold")

    direction = "LONG" if digit_dir == "OVER" else "SHORT"
    score = raw_score / 8.0

    if score >= 0.875:
        strength = 3
    elif score >= 0.625:
        strength = 2
    else:
        logger.info(f"REJECTED: {symbol} DIGIT strength=1 score={score:.3f} — below threshold")
        return SignalResult("NONE", 0, score, "DIGIT", "Below entry threshold")

    logger.info(f"DIGIT: {symbol} {direction} score={raw_score}/8")
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy="DIGIT",
        reason=f"Digit {digit_dir} raw={raw_score}/8",
    )


# ---------------------------------------------------------------------------
# Strategy 2 — Mean Reversion
# ---------------------------------------------------------------------------

def evaluate_mean_reversion(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    if len(ltf_bars) < 25:
        return NONE_RESULT

    C, H, L = _arrays(ltf_bars)
    rsi = ind.rsi(C, 14)
    upper, mid, lower = ind.bollinger_bands(C, 20, 2.0)
    roc = ind.roc(C, 10)

    last_rsi   = _last(rsi)
    last_close = float(C[-1])
    last_upper = _last(upper)
    last_lower = _last(lower)
    last_roc   = _last(roc)

    long_score = 0
    long_all = True
    if last_rsi < 22:
        long_score += 3
    else:
        long_all = False
    if last_close <= last_lower:
        long_score += 3
    else:
        long_all = False
    if last_roc < -0.02:
        long_score += 2
    else:
        long_all = False

    short_score = 0
    short_all = True
    if last_rsi > 78:
        short_score += 3
    else:
        short_all = False
    if last_close >= last_upper:
        short_score += 3
    else:
        short_all = False
    if last_roc > 0.02:
        short_score += 2
    else:
        short_all = False

    if long_score >= 6 and long_score >= short_score:
        raw = long_score
        direction = "LONG"
        all_met = long_all
    elif short_score >= 6:
        raw = short_score
        direction = "SHORT"
        all_met = short_all
    else:
        best = max(long_score, short_score)
        logger.debug(f"REJECTED: {symbol} MEAN_REV strength=0 score={best/8.0:.3f} — below threshold")
        return SignalResult("NONE", 0, best / 8.0, "MEAN_REV", "Below entry threshold")

    score = raw / 8.0
    strength = 3 if all_met else (2 if score >= 6 / 8.0 else 1)
    if strength <= 1:
        logger.info(f"REJECTED: {symbol} MEAN_REV strength=1 score={score:.3f} — below threshold")
        return SignalResult("NONE", 0, score, "MEAN_REV", "Below entry threshold")

    logger.info(
        f"SIGNAL: {symbol} {direction} MEAN_REV strength={strength} score={score:.3f}"
    )
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy="MEAN_REV",
        reason=f"MeanRev RSI={last_rsi:.1f} raw={raw}/8 (70.8% documented win rate)",
    )


# ---------------------------------------------------------------------------
# Strategy 3 — Range Break Retest
# ---------------------------------------------------------------------------

def evaluate_range_break(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    if len(ltf_bars) < 30:
        return NONE_RESULT

    C, H, L = _arrays(ltf_bars)
    rsi = ind.rsi(C, 14)
    atr = ind.atr(H, L, C, 14)
    last_atr = _last(atr)
    last_rsi = _last(rsi)

    consolidation = ind.find_consolidation(H, L, C)
    cons_upper, cons_lower = (None, None)
    has_consolidation = consolidation is not None
    if has_consolidation:
        cons_upper, cons_lower = consolidation

    # --- Phase A: find most recent breakout within last 3 bars ---
    breakout_dir: Optional[str] = None
    breakout_level: Optional[float] = None
    breakout_bars_ago: Optional[int] = None

    search_bounds = (cons_upper, cons_lower) if has_consolidation else None
    if search_bounds is None:
        # Fall back to a rolling range if no consolidation zone was found,
        # so breakout/retest logic still has a boundary to test against.
        lookback = min(20, len(C) - 4)
        search_upper = float(np.max(H[-lookback - 4:-4])) if lookback > 0 else float(H[-4])
        search_lower = float(np.min(L[-lookback - 4:-4])) if lookback > 0 else float(L[-4])
    else:
        search_upper, search_lower = search_bounds

    for bars_ago in range(1, 4):  # 1, 2, 3 bars old
        idx = -bars_ago
        close_i = float(C[idx])
        if close_i > search_upper + 0.3 * last_atr:
            breakout_dir = "LONG"
            breakout_level = search_upper
            breakout_bars_ago = bars_ago
            break
        if close_i < search_lower - 0.3 * last_atr:
            breakout_dir = "SHORT"
            breakout_level = search_lower
            breakout_bars_ago = bars_ago
            break

    if breakout_dir is None:
        logger.debug(f"REJECTED: {symbol} RANGE_BREAK strength=0 score=0.000 — below threshold")
        return SignalResult("NONE", 0, 0.0, "RANGE_BREAK", "No breakout detected")

    # --- Phase B: retest ---
    current_price = float(C[-1])
    retested = abs(current_price - breakout_level) <= 0.5 * last_atr

    if not retested:
        logger.debug(f"REJECTED: {symbol} RANGE_BREAK strength=0 score=0.250 — below threshold")
        return SignalResult("NONE", 0, 0.25, "RANGE_BREAK", "Breakout found, awaiting retest")

    rsi_confirmed = (last_rsi > 52) if breakout_dir == "LONG" else (last_rsi < 48)

    confirmed = 1  # breakout confirmed
    confirmed += 1  # retest confirmed
    if rsi_confirmed:
        confirmed += 1
    if has_consolidation:
        confirmed += 1

    if not rsi_confirmed:
        logger.info(f"REJECTED: {symbol} RANGE_BREAK strength=1 score={confirmed/4.0:.3f} — below threshold")
        return SignalResult("NONE", 0, confirmed / 4.0, "RANGE_BREAK", "RSI not confirmed")

    strength = 3 if (has_consolidation and rsi_confirmed) else 2
    score = confirmed / 4.0

    logger.info(
        f"SIGNAL: {symbol} {breakout_dir} RANGE_BREAK strength={strength} score={score:.3f}"
    )
    return SignalResult(
        direction=breakout_dir,
        strength=strength,
        score=score,
        strategy="RANGE_BREAK",
        reason=(
            f"Breakout {breakout_bars_ago}bars ago @ {breakout_level:.5f}, "
            f"retest confirmed, RSI={last_rsi:.1f}, consolidation={has_consolidation}"
        ),
    )


# ---------------------------------------------------------------------------
# Strategy 4 — Post-Spike Fade (Boom/Crash)
# ---------------------------------------------------------------------------

def evaluate_boom_crash(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    if len(ltf_bars) < 20:
        return NONE_RESULT

    C, H, L = _arrays(ltf_bars)
    rsi = ind.rsi(C, 14)
    atr = ind.atr(H, L, C, 14)
    last_atr = _last(atr) or 0.001
    last_rsi = _last(rsi)

    spike = ind.detect_spike(C, H, L, lookback=12)
    # Expected spike shape: {"detected": bool, "type": "BOOM"|"CRASH",
    #                        "size": float, "bars_ago": int}
    if not spike or not spike.get("detected"):
        return NONE_RESULT

    bars_ago = spike.get("bars_ago", 0)
    spike_type = spike.get("type")
    spike_size = float(spike.get("size", 0.0))

    # Must be within last 2 bars, but at least 1 bar since the spike bar closed
    if bars_ago < 1 or bars_ago > 2:
        logger.debug(f"REJECTED: {symbol} BOOM_CRASH strength=0 score=0.000 — below threshold")
        return NONE_RESULT

    # Cooldown: no earlier spike in the preceding 10 bars
    earlier_spike = ind.detect_spike(
        C[: -bars_ago] if bars_ago > 0 else C,
        H[: -bars_ago] if bars_ago > 0 else H,
        L[: -bars_ago] if bars_ago > 0 else L,
        lookback=10,
    )
    if earlier_spike and earlier_spike.get("detected"):
        logger.info(f"REJECTED: {symbol} BOOM_CRASH strength=1 score=0.000 — below threshold")
        return NONE_RESULT

    if spike_type == "BOOM":
        direction = "SHORT"
        rsi_confirmed = last_rsi > 60
    elif spike_type == "CRASH":
        direction = "LONG"
        rsi_confirmed = last_rsi < 40
    else:
        return NONE_RESULT

    strength = 3 if rsi_confirmed else 2
    score = min(spike_size / (last_atr * 5.0), 1.0)

    logger.info(
        f"SIGNAL: {symbol} {direction} BOOM_CRASH strength={strength} score={score:.3f}"
    )
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy="BOOM_CRASH",
        reason=f"Fade {spike_type} spike size={spike_size:.5f} RSI={last_rsi:.1f}",
    )


# ---------------------------------------------------------------------------
# Strategy 5 — Step Index Trend
# ---------------------------------------------------------------------------

def evaluate_step(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    if len(ltf_bars) < 35:
        return NONE_RESULT

    C, H, L = _arrays(ltf_bars)
    ema10 = ind.ema(C, 10)
    ema30 = ind.ema(C, 30)
    donchian_upper, donchian_lower = ind.donchian(H, L, 20)

    e10, e30 = ema10[-1], ema30[-1]
    e10_prev = ema10[-2]

    ema_dir: Optional[str] = None
    if e10 > e30 and e10 > e10_prev:
        ema_dir = "LONG"
    elif e10 < e30 and e10 < e10_prev:
        ema_dir = "SHORT"

    last_close = float(C[-1])
    last_don_upper = _last(donchian_upper)
    last_don_lower = _last(donchian_lower)

    donchian_dir: Optional[str] = None
    if last_close >= last_don_upper:
        donchian_dir = "SHORT"
    elif last_close <= last_don_lower:
        donchian_dir = "LONG"

    if ema_dir is None or donchian_dir is None or ema_dir != donchian_dir:
        logger.debug(f"REJECTED: {symbol} STEP strength=0 score=0.000 — below threshold")
        return NONE_RESULT

    direction = ema_dir
    strength = 2
    score = 0.65

    logger.info(
        f"SIGNAL: {symbol} {direction} STEP strength={strength} score={score:.3f}"
    )
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy="STEP",
        reason=f"EMA10/30 trend + Donchian band agreement ({direction})",
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SignalEngine:

    def __init__(self, *args, **kwargs):
        pass

    def evaluate(self, ltf_bars: List[Candle], symbol: str, **kwargs) -> SignalResult:
        if symbol in config.DIGIT_SYMBOLS:
            result = evaluate_digit(ltf_bars, symbol)
        elif symbol in config.MEAN_REVERSION_SYMBOLS:
            result = evaluate_mean_reversion(ltf_bars, symbol)
        elif symbol in config.RANGE_BREAK_SYMBOLS:
            result = evaluate_range_break(ltf_bars, symbol)
        elif symbol in config.BOOM_CRASH_SYMBOLS:
            result = evaluate_boom_crash(ltf_bars, symbol)
        elif symbol in config.STEP_SYMBOLS:
            result = evaluate_step(ltf_bars, symbol)
        else:
            logger.debug(f"REJECTED: {symbol} UNROUTED strength=0 score=0.000 — below threshold")
            return NONE_RESULT

        if result.strength >= 2:
            logger.info(
                f"SIGNAL: {symbol} {result.direction} {result.strategy} "
                f"strength={result.strength} score={result.score:.3f}"
            )
            return result

        logger.info(
            f"REJECTED: {symbol} {result.strategy} strength={result.strength} "
            f"score={result.score:.3f} — below threshold"
        )
        return NONE_RESULT

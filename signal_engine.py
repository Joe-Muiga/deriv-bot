"""
signal_engine.py — Deriv Trading Bot Signal Engine
v14 — Five independent strategy evaluators.

All previous SMC, order block, and HTF bias logic has been removed entirely.

STRATEGIES:
    1. evaluate_digit          → config.        (Digit Over/Under)
    2. evaluate_mean_reversion → config.MEAN_REVERSION_SYMBOLS (Mean Reversion)
    3. evaluate_range_break    → config.RANGE_BREAK_SYMBOLS  (Range Break Retest)
    4. evaluate_boom_crash     → config.BOOM_CRASH_SYMBOLS   (Post-Spike Fade)
    5. evaluate_step           → config.STEP_SYMBOLS         (Step Index Trend)

EMISSION RULES (absolute law):
    strength = 3  → ALWAYS emit.   Zero further checks.
    strength = 2  → ALWAYS emit.   Score used for ranking only.
    strength ≤ 1  → Always reject.
"""

import logging
import time
from dataclasses import dataclass
from typing import List

import numpy as np

import config
import indicators as ind
from candlestick_builder import Candle

logger = logging.getLogger(__name__)


# ─── SignalResult ──────────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    direction: str    # "LONG" | "SHORT" | "NONE"
    strength:  int    # 1-3
    score:     float  # 0.0-1.0 composite probability
    strategy:  str    # which strategy fired
    reason:    str    # human readable


def _none(strategy: str, reason: str) -> SignalResult:
    """Convenience constructor for a rejected / no-signal result."""
    return SignalResult(
        direction="NONE",
        strength=0,
        score=0.0,
        strategy=strategy,
        reason=reason,
    )


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _closes(bars: List[Candle]) -> np.ndarray:
    return np.array([float(b.close) for b in bars], dtype=np.float64)


def _highs(bars: List[Candle]) -> np.ndarray:
    return np.array([float(b.high) for b in bars], dtype=np.float64)


def _lows(bars: List[Candle]) -> np.ndarray:
    return np.array([float(b.low) for b in bars], dtype=np.float64)


def _last(arr: np.ndarray) -> float:
    """Return last non-NaN value or NaN if none exists."""
    valid = arr[~np.isnan(arr)]
    return float(valid[-1]) if len(valid) else float("nan")


# ─── Strategy 1 — Digit Over/Under ───────────────────────────────────────────

def evaluate_digit(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    """
    Applies to: config.DIGIT_SYMBOLS

    Indicators: RSI14, Bollinger Bands 20, Rate of Change 10
    Gate: ind.digit_score() must return raw_score ≥ 6

    OVER  → direction = LONG  (price will rise above digit threshold)
    UNDER → direction = SHORT

    Score    = raw_score / 8.0
    Strength = 3 if score ≥ 0.875 else 2 if score ≥ 0.625 else reject
    """
    strategy = "DIGIT"

    if len(ltf_bars) < 25:
        return _none(strategy, "insufficient bars")

    C = _closes(ltf_bars)

    # Compute auxiliary indicators (required by contract; passed to digit_score)
    rsi14 = ind.rsi(C, 14)
    bb_upper, bb_mid, bb_lower = ind.bollinger_bands(C, 20, 2)
    roc10 = ind.roc(C, 10)

    # Call digit_score — returns (signal_str, raw_score)
    # Expected: signal_str in {"OVER", "UNDER", "NONE"}, raw_score int 0-8
    try:
        signal_str, raw_score = ind.digit_score(
            C,
            rsi=rsi14,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            roc=roc10,
        )
    except Exception as exc:
        logger.warning(f"DIGIT [{symbol}] digit_score() error: {exc}")
        return _none(strategy, f"digit_score error: {exc}")

    logger.debug(f"DIGIT: {symbol} {signal_str} score={raw_score}/8")

    if raw_score < 6:
        return _none(strategy, f"digit_score {raw_score}/8 < minimum 6")

    if signal_str == "OVER":
        direction = "LONG"
    elif signal_str == "UNDER":
        direction = "SHORT"
    else:
        return _none(strategy, f"digit_score returned NONE at {raw_score}/8")

    score = raw_score / 8.0

    if score >= 0.875:
        strength = 3
    elif score >= 0.625:
        strength = 2
    else:
        return _none(strategy, f"score {score:.3f} below 0.625 threshold")

    logger.info(f"DIGIT: {symbol} {direction} score={raw_score}/8")
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy=strategy,
        reason=f"digit_score={raw_score}/8 score={score:.3f}",
    )


# ─── Strategy 2 — Mean Reversion ─────────────────────────────────────────────

def evaluate_mean_reversion(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    """
    Applies to: config.MEAN_REVERSION_SYMBOLS

    Indicators: RSI14, Bollinger Bands 20, Rate of Change 10

    LONG  conditions: RSI < 22  (+3 pts)
                      close ≤ BB_lower (+3 pts)
                      ROC < -0.02 (+2 pts)

    SHORT conditions: RSI > 78  (+3 pts)
                      close ≥ BB_upper (+3 pts)
                      ROC > +0.02 (+2 pts)

    Minimum 6 pts to emit (documented 70.8% win-rate threshold).
    Score    = raw / 8.0
    Strength = 3 if all three conditions met, 2 if score ≥ 6, else reject.
    """
    strategy = "MEAN_REVERSION"

    if len(ltf_bars) < 25:
        return _none(strategy, "insufficient bars")

    C = _closes(ltf_bars)
    close = C[-1]

    rsi_arr = ind.rsi(C, 14)
    rsi_val = _last(rsi_arr)
    if np.isnan(rsi_val):
        return _none(strategy, "RSI14 unavailable")

    bb_upper_arr, _, bb_lower_arr = ind.bollinger_bands(C, 20, 2)
    bb_upper = _last(bb_upper_arr)
    bb_lower = _last(bb_lower_arr)
    if np.isnan(bb_upper) or np.isnan(bb_lower):
        return _none(strategy, "Bollinger Bands unavailable")

    roc_arr = ind.roc(C, 10)
    roc_val = _last(roc_arr)
    if np.isnan(roc_val):
        return _none(strategy, "ROC10 unavailable")

    # Score LONG conditions
    long_pts = 0
    long_all = True
    if rsi_val < 22:
        long_pts += 3
    else:
        long_all = False
    if close <= bb_lower:
        long_pts += 3
    else:
        long_all = False
    if roc_val < -0.02:
        long_pts += 2
    else:
        long_all = False

    # Score SHORT conditions
    short_pts = 0
    short_all = True
    if rsi_val > 78:
        short_pts += 3
    else:
        short_all = False
    if close >= bb_upper:
        short_pts += 3
    else:
        short_all = False
    if roc_val > 0.02:
        short_pts += 2
    else:
        short_all = False

    logger.debug(
        f"MEAN_REV [{symbol}] RSI={rsi_val:.2f} close={close:.5f} "
        f"BB_upper={bb_upper:.5f} BB_lower={bb_lower:.5f} ROC={roc_val:.5f} "
        f"long_pts={long_pts} short_pts={short_pts}"
    )

    if long_pts >= short_pts and long_pts >= 6:
        direction = "LONG"
        raw = long_pts
        all_met = long_all
    elif short_pts > long_pts and short_pts >= 6:
        direction = "SHORT"
        raw = short_pts
        all_met = short_all
    else:
        best = max(long_pts, short_pts)
        return _none(strategy, f"best raw score {best}/8 below minimum 6")

    score = raw / 8.0
    strength = 3 if all_met else 2

    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy=strategy,
        reason=(
            f"RSI={rsi_val:.2f} close={close:.5f} "
            f"BB_upper={bb_upper:.5f} BB_lower={bb_lower:.5f} "
            f"ROC={roc_val:.5f} raw={raw}/8 score={score:.3f}"
        ),
    )


# ─── Strategy 3 — Range Break Retest ─────────────────────────────────────────

def evaluate_range_break(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    """
    Applies to: config.RANGE_BREAK_SYMBOLS

    Phase A — detect breakout:
        Call ind.find_consolidation().  Check if latest close exceeds the
        consolidation boundary by > 0.3× ATR14.  Breakout must be ≤ 3 bars old.

    Phase B — wait for retest:
        Price has pulled back within 0.5× ATR of the broken boundary level.

    RSI14 confirms: > 52 bullish, < 48 bearish.

    Strength 3: consolidation + breakout + retest + RSI all confirmed.
    Strength 2: breakout + retest + RSI (no consolidation).
    Strength ≤ 1: reject.
    Score = confirmed_conditions / 4.0
    """
    strategy = "RANGE_BREAK"

    if len(ltf_bars) < 30:
        return _none(strategy, "insufficient bars")

    C = _closes(ltf_bars)
    H = _highs(ltf_bars)
    L = _lows(ltf_bars)
    close = C[-1]

    # ATR14
    atr_arr = ind.atr(H, L, C, 14)
    atr14 = _last(atr_arr)
    if np.isnan(atr14) or atr14 == 0:
        return _none(strategy, "ATR14 unavailable")

    # RSI14
    rsi_arr = ind.rsi(C, 14)
    rsi_val = _last(rsi_arr)
    if np.isnan(rsi_val):
        return _none(strategy, "RSI14 unavailable")

    # Phase A — consolidation
    consolidation_confirmed = False
    consol_level = None       # broken boundary level
    consol_direction = None   # "LONG" | "SHORT"
    breakout_bar_age = None

    try:
        consol_result = ind.find_consolidation(ltf_bars)
        # Expected: dict with keys "found", "upper", "lower", "breakout_bar_index"
        # breakout_bar_index is index from the end (0 = latest bar)
        if consol_result and consol_result.get("found"):
            c_upper = float(consol_result["upper"])
            c_lower = float(consol_result["lower"])
            brk_idx = int(consol_result.get("breakout_bar_index", 999))
            breakout_bar_age = brk_idx  # bars since breakout

            if breakout_bar_age <= 3:
                if close > c_upper + 0.3 * atr14:
                    consol_level = c_upper
                    consol_direction = "LONG"
                    consolidation_confirmed = True
                elif close < c_lower - 0.3 * atr14:
                    consol_level = c_lower
                    consol_direction = "SHORT"
                    consolidation_confirmed = True

    except Exception as exc:
        logger.debug(f"RANGE_BREAK [{symbol}] find_consolidation error: {exc}")
        # Consolidation detection failed; proceed to Phase B without it

    # Phase B — retest (without consolidation context if Phase A failed)
    # If no consolidation found, attempt to find a recent swing high/low breakout
    retest_confirmed = False
    breakout_confirmed = False

    if consolidation_confirmed and consol_level is not None:
        breakout_confirmed = True
        # Retest: price has pulled back within 0.5× ATR of the broken boundary
        if abs(close - consol_level) <= 0.5 * atr14:
            retest_confirmed = True
    else:
        # No consolidation — check for raw breakout via recent swing
        # Look for a level breach in last 3 bars
        if len(C) >= 5:
            lookback_C = C[-5:-1]   # bars 4..1 ago (exclude latest)
            swing_high = float(np.max(lookback_C))
            swing_low = float(np.min(lookback_C))

            if close > swing_high + 0.3 * atr14:
                breakout_confirmed = True
                consol_direction = "LONG"
                consol_level = swing_high
                if abs(close - swing_high) <= 0.5 * atr14:
                    retest_confirmed = True
            elif close < swing_low - 0.3 * atr14:
                breakout_confirmed = True
                consol_direction = "SHORT"
                consol_level = swing_low
                if abs(close - swing_low) <= 0.5 * atr14:
                    retest_confirmed = True

    if not breakout_confirmed or not retest_confirmed:
        missing = []
        if not breakout_confirmed:
            missing.append("no breakout")
        if not retest_confirmed:
            missing.append("no retest")
        return _none(strategy, "; ".join(missing))

    # RSI confirmation
    rsi_confirmed = False
    if consol_direction == "LONG" and rsi_val > 52:
        rsi_confirmed = True
    elif consol_direction == "SHORT" and rsi_val < 48:
        rsi_confirmed = True

    if not rsi_confirmed:
        return _none(
            strategy,
            f"RSI={rsi_val:.2f} does not confirm {consol_direction} retest",
        )

    # Count confirmed conditions (max 4)
    confirmed = sum([
        consolidation_confirmed,
        breakout_confirmed,
        retest_confirmed,
        rsi_confirmed,
    ])

    score = confirmed / 4.0

    if consolidation_confirmed:
        strength = 3
    else:
        # breakout + retest + RSI = 3 conditions but no consolidation
        strength = 2

    direction = consol_direction

    logger.debug(
        f"RANGE_BREAK [{symbol}] {direction} "
        f"consol={consolidation_confirmed} breakout={breakout_confirmed} "
        f"retest={retest_confirmed} RSI_ok={rsi_confirmed} "
        f"score={score:.3f} strength={strength}"
    )

    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy=strategy,
        reason=(
            f"consolidation={consolidation_confirmed} breakout=True "
            f"retest=True RSI={rsi_val:.2f} "
            f"boundary={consol_level:.5f} ATR={atr14:.5f} "
            f"confirmed={confirmed}/4"
        ),
    )


# ─── Strategy 4 — Post-Spike Fade (Boom/Crash) ───────────────────────────────

def evaluate_boom_crash(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    """
    Applies to: config.BOOM_CRASH_SYMBOLS

    Spike detection: ind.detect_spike() — must be within last 2 bars.
    Cooldown: no spike in last 10 bars before the spike bar.

    After BOOM spike  → direction = SHORT (fade down)
    After CRASH spike → direction = LONG  (fade up)

    RSI14 confirmation:
        BOOM  spike → RSI > 60
        CRASH spike → RSI < 40

    Wait 1 bar after spike bar closes before emitting.

    Strength 3: spike + RSI extreme confirmed.
    Strength 2: spike confirmed but RSI not yet extreme.
    Score = spike_size / (ATR14 × 5), capped at 1.0.

    If no spike: return NONE — never generate directional signals without spike.
    """
    strategy = "BOOM_CRASH"

    if len(ltf_bars) < 15:
        return _none(strategy, "insufficient bars")

    C = _closes(ltf_bars)
    H = _highs(ltf_bars)
    L = _lows(ltf_bars)

    # ATR14
    atr_arr = ind.atr(H, L, C, 14)
    atr14 = _last(atr_arr)
    if np.isnan(atr14) or atr14 == 0:
        return _none(strategy, "ATR14 unavailable")

    # RSI14
    rsi_arr = ind.rsi(C, 14)
    rsi_val = _last(rsi_arr)
    if np.isnan(rsi_val):
        return _none(strategy, "RSI14 unavailable")

    # Spike detection
    # ind.detect_spike() expected to return:
    #   dict: {"found": bool, "type": "BOOM"|"CRASH", "bar_index": int, "size": float}
    #   bar_index = bars from end (0 = latest bar, 1 = previous bar, …)
    try:
        spike_result = ind.detect_spike(ltf_bars)
    except Exception as exc:
        logger.warning(f"BOOM_CRASH [{symbol}] detect_spike() error: {exc}")
        return _none(strategy, f"detect_spike error: {exc}")

    if not spike_result or not spike_result.get("found"):
        return _none(strategy, "no spike detected — NONE required without spike")

    spike_type = spike_result.get("type", "")       # "BOOM" or "CRASH"
    bar_index = int(spike_result.get("bar_index", 999))  # bars from end
    spike_size = float(spike_result.get("size", 0.0))

    # Spike must be within last 2 bars
    if bar_index > 2:
        return _none(strategy, f"spike too old ({bar_index} bars ago, max=2)")

    # Wait 1 bar after spike bar closes — spike bar itself is index 0 (latest),
    # so we require bar_index ≥ 1 (spike occurred on previous bar)
    if bar_index == 0:
        return _none(strategy, "spike on current unclosed bar — waiting 1 bar")

    # Cooldown: no spike in the 10 bars before the spike bar
    # The spike bar is at position bar_index from the end.
    # The 10 bars before that span from (bar_index + 1) to (bar_index + 10).
    cooldown_start = bar_index + 1
    cooldown_end = bar_index + 11  # exclusive
    cooldown_bars = ltf_bars[max(0, len(ltf_bars) - cooldown_end):
                              max(0, len(ltf_bars) - cooldown_start)]
    if len(cooldown_bars) > 0:
        try:
            cooldown_spike = ind.detect_spike(cooldown_bars)
            if cooldown_spike and cooldown_spike.get("found"):
                return _none(strategy,
                             "cooldown active — spike within last 10 bars before this one")
        except Exception:
            pass  # If cooldown check fails, proceed conservatively

    # Direction (fade the spike)
    if spike_type == "BOOM":
        direction = "SHORT"
        rsi_extreme = rsi_val > 60
    elif spike_type == "CRASH":
        direction = "LONG"
        rsi_extreme = rsi_val < 40
    else:
        return _none(strategy, f"unknown spike type '{spike_type}'")

    # Score
    score = min(1.0, spike_size / (atr14 * 5))

    # Strength
    strength = 3 if rsi_extreme else 2

    logger.debug(
        f"BOOM_CRASH [{symbol}] {spike_type} → {direction} "
        f"bar_index={bar_index} spike_size={spike_size:.5f} "
        f"ATR={atr14:.5f} RSI={rsi_val:.2f} rsi_extreme={rsi_extreme} "
        f"score={score:.3f} strength={strength}"
    )

    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy=strategy,
        reason=(
            f"{spike_type} spike bar_index={bar_index} size={spike_size:.5f} "
            f"ATR={atr14:.5f} RSI={rsi_val:.2f} rsi_extreme={rsi_extreme}"
        ),
    )


# ─── Strategy 5 — Step Index Trend ───────────────────────────────────────────

def evaluate_step(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    """
    Applies to: config.STEP_SYMBOLS

    EMA10 > EMA30 and rising  → LONG
    EMA10 < EMA30 and falling → SHORT

    Donchian Channel (20): price at upper band → SHORT, lower band → LONG
    Both must agree for signal to emit.

    Strength = 2 always (Step Index has no high-confidence entry).
    Score    = 0.65 always.
    """
    strategy = "STEP"

    if len(ltf_bars) < 35:
        return _none(strategy, "insufficient bars")

    C = _closes(ltf_bars)
    H = _highs(ltf_bars)
    L = _lows(ltf_bars)
    close = C[-1]

    # EMA10 and EMA30
    ema10_arr = ind.ema(C, 10)
    ema30_arr = ind.ema(C, 30)

    valid_ema10 = ema10_arr[~np.isnan(ema10_arr)]
    valid_ema30 = ema30_arr[~np.isnan(ema30_arr)]

    if len(valid_ema10) < 2 or len(valid_ema30) < 1:
        return _none(strategy, "EMA10/30 unavailable")

    ema10_curr = float(valid_ema10[-1])
    ema10_prev = float(valid_ema10[-2])
    ema30_curr = float(valid_ema30[-1])

    ema_rising  = ema10_curr > ema10_prev
    ema_falling = ema10_curr < ema10_prev

    if ema10_curr > ema30_curr and ema_rising:
        ema_signal = "LONG"
    elif ema10_curr < ema30_curr and ema_falling:
        ema_signal = "SHORT"
    else:
        return _none(
            strategy,
            f"EMA10={ema10_curr:.5f} EMA30={ema30_curr:.5f} "
            f"rising={ema_rising} — no clear EMA trend",
        )

    # Donchian Channel (20-bar)
    try:
        dc_upper_arr, dc_lower_arr = ind.donchian_channel(H, L, 20)
        dc_upper = _last(dc_upper_arr)
        dc_lower = _last(dc_lower_arr)
        if np.isnan(dc_upper) or np.isnan(dc_lower):
            raise ValueError("NaN")
    except Exception as exc:
        return _none(strategy, f"Donchian channel unavailable: {exc}")

    # Determine Donchian signal:
    # price at upper band → SHORT (mean reversion / trend exhaustion on step)
    # price at lower band → LONG
    dc_range = dc_upper - dc_lower if dc_upper != dc_lower else 1.0
    upper_proximity = (close - dc_lower) / dc_range   # 0 = at lower, 1 = at upper

    if upper_proximity >= 0.85:
        dc_signal = "SHORT"
    elif upper_proximity <= 0.15:
        dc_signal = "LONG"
    else:
        return _none(
            strategy,
            f"price not at Donchian extreme (proximity={upper_proximity:.3f})",
        )

    if ema_signal != dc_signal:
        return _none(
            strategy,
            f"EMA signal={ema_signal} disagrees with Donchian signal={dc_signal}",
        )

    direction = ema_signal
    strength = 2
    score = 0.65

    logger.debug(
        f"STEP [{symbol}] {direction} "
        f"EMA10={ema10_curr:.5f} EMA30={ema30_curr:.5f} "
        f"DC_upper={dc_upper:.5f} DC_lower={dc_lower:.5f} "
        f"close={close:.5f} proximity={upper_proximity:.3f}"
    )

    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy=strategy,
        reason=(
            f"EMA10={ema10_curr:.5f} EMA30={ema30_curr:.5f} "
            f"DC_upper={dc_upper:.5f} DC_lower={dc_lower:.5f} "
            f"proximity={upper_proximity:.3f}"
        ),
    )


# ─── SignalEngine ─────────────────────────────────────────────────────────────

class SignalEngine:
    """
    Routes each symbol to its designated strategy evaluator and enforces
    emission rules.

    EMISSION RULES (absolute law):
        strength = 3  → ALWAYS emit.  Zero further checks.
        strength = 2  → ALWAYS emit.  Score used for ranking only.
        strength ≤ 1  → Always reject.
    """

    def __init__(self, symbols: list = None, **kwargs):
        self.symbols = symbols or []

    def evaluate(
        self,
        ltf_bars: List[Candle],
        symbol: str = "",
        **kwargs,                  # absorbs legacy kwargs (htf_bias, smc_ctx, etc.)
    ) -> SignalResult:
        """
        Route to the correct strategy based on which config list symbol belongs
        to, then enforce emission rules.
        """
        sym = symbol or "UNKNOWN"

        # ── Route to strategy ──────────────────────────────────────────────────
        result = self._route(ltf_bars, sym)

        # ── Enforce emission rules ─────────────────────────────────────────────
        if result.strength >= 3:
            # Strength 3 — unconditional emit
            logger.info(
                f"SIGNAL: {sym} {result.direction} {result.strategy} "
                f"strength={result.strength} score={result.score:.3f}"
            )
            return result

        if result.strength == 2:
            # Strength 2 — always emit; score used for ranking only
            logger.info(
                f"SIGNAL: {sym} {result.direction} {result.strategy} "
                f"strength={result.strength} score={result.score:.3f}"
            )
            return result

        # Strength ≤ 1 — always reject
        logger.info(
            f"REJECTED: {sym} {result.strategy} "
            f"strength={result.strength} score={result.score:.3f} "
            f"— below threshold"
        )
        return SignalResult(
            direction="NONE",
            strength=result.strength,
            score=result.score,
            strategy=result.strategy,
            reason=result.reason,
        )

    def _route(self, ltf_bars: List[Candle], symbol: str) -> SignalResult:
        """
        Dispatch to the correct evaluator based on config symbol lists.
        A symbol may appear in at most one list; first match wins.
        """
        if hasattr(config, "DIGIT_SYMBOLS") and symbol in config.DIGIT_SYMBOLS:
            return evaluate_digit(ltf_bars, symbol)

        if (hasattr(config, "MEAN_REVERSION_SYMBOLS")
                and symbol in config.MEAN_REVERSION_SYMBOLS):
            return evaluate_mean_reversion(ltf_bars, symbol)

        if (hasattr(config, "RANGE_BREAK_SYMBOLS")
                and symbol in config.RANGE_BREAK_SYMBOLS):
            return evaluate_range_break(ltf_bars, symbol)

        if (hasattr(config, "BOOM_CRASH_SYMBOLS")
                and symbol in config.BOOM_CRASH_SYMBOLS):
            return evaluate_boom_crash(ltf_bars, symbol)

        if hasattr(config, "STEP_SYMBOLS") and symbol in config.STEP_SYMBOLS:
            return evaluate_step(ltf_bars, symbol)

        logger.warning(
            f"SignalEngine: '{symbol}' not found in any config symbol list — "
            f"returning NONE"
        )
        return _none("UNKNOWN", f"symbol '{symbol}' not in any configured strategy list")

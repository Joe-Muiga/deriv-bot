"""
Multi-strategy signal engine.
Each symbol is routed to exactly ONE strategy evaluator based on which
config symbol-list it belongs to. No cross-strategy voting, no SMC,
no order blocks, no HTF bias — each category gets its own independent
purpose-built evaluator.

Additions in this pass (all tick-based, independent of the candle-based
evaluators above them):
  - evaluate_digit_parity      : chi-square even/odd bias on raw ticks
  - evaluate_digit (modified)  : optional hybrid RSI/BB/ROC + chi-square gate
  - evaluate_drift_fade        : Boom/Crash drift-following (post-cooldown)
  - evaluate_jump_buildup      : Jump index build-up confidence -> digit contract (MATCH/DIFFER)
  - evaluate_trend_shift       : Bear/Bull fixed per-symbol daily-reset bias (RDBULL=LONG, RDBEAR=SHORT)

See the "NEW STRATEGY CONFIG" region below for the config keys these read
(all via getattr with safe defaults, so nothing breaks if unset) and the
chat reply for a full list of flagged inconsistencies/assumptions.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

import config
import indicators as ind
from candlestick_builder import Candle
from symbol_manager import SymbolManager

logger = logging.getLogger(__name__)

# Implementation Brief v3, finding #4 / task 1: single shared instance so
# evaluate_trend_shift()'s post-reset timing check calls symbol_manager's
# own is_post_reset()/get_bear_bull_state() instead of re-implementing the
# same "minutes since 00:00 GMT" math a second time. is_post_reset() reads
# only module-level config (BEAR_BULL_SYMBOLS, BEAR_BULL_TREND_SHIFT_MINS)
# and wall-clock time — it doesn't touch any of SymbolManager's mutable
# per-symbol state (suspensions, session counters, etc.) — so a private
# instance here is safe and doesn't need to be the same object bot_engine.py
# holds as self.symbols.
_symbol_manager = SymbolManager()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    direction: str    # "LONG" | "SHORT" | "NONE" | "MATCH" | "DIFFER"
    strength:  int    # 1-3
    score:     float  # 0.0-1.0 composite probability
    strategy:  str    # which strategy fired
    reason:    str    # human readable
    # Implementation Brief v3, finding #3 / task 2: JUMP_BUILDUP's real
    # recommendation is a digit contract (Matches/Differs), not a price
    # direction — these three fields let bot_engine._execute() route to
    # DerivClient.buy_digit_contract() instead of buy_contract() (CALL/PUT)
    # without every other evaluator's call site needing to change.
    # contract_kind defaults to "RISE_FALL" so every existing evaluator
    # (which only sets direction/strength/score/strategy/reason) is
    # unaffected.
    contract_kind: str            = "RISE_FALL"   # "RISE_FALL" | "DIGIT"
    digit:         Optional[int]  = None            # 0-9, DIGIT contracts only
    match_type:    Optional[str]  = None            # "MATCH" | "DIFFER"


NONE_RESULT = SignalResult("NONE", 0, 0.0, "NONE", "No signal")


# ---------------------------------------------------------------------------
# Helpers — candle arrays (existing)
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
# Helpers — raw ticks (new)
#
# ASSUMPTION: no tick-buffer type is defined anywhere in the three files
# provided (config.py / symbol_manager.py / signal_engine.py), and
# bot_engine.py / deriv_client.py were not included in this pass, so the
# shape of "ticks" as produced by the rest of the bot is unknown. These
# helpers duck-type against the three shapes most likely to occur:
#   - Deriv's raw tick dict:      {"epoch": ..., "quote": ...}
#   - A lightweight object:       obj.epoch / obj.quote (or .price)
#   - A plain (epoch, quote) tuple/list
#   - A bare float (quote only — epoch-dependent functions degrade
#     gracefully, see evaluate_jump_buildup)
# If the real tick buffer differs from all of these, only these two
# helpers need updating — every new function below goes through them.
# ---------------------------------------------------------------------------

def _tick_quote(t: Any) -> float:
    if isinstance(t, (int, float, np.floating)):
        return float(t)
    if isinstance(t, dict):
        q = t.get("quote", t.get("price"))
        if q is not None:
            return float(q)
    q = getattr(t, "quote", None)
    if q is None:
        q = getattr(t, "price", None)
    if q is not None:
        return float(q)
    if isinstance(t, (tuple, list)) and len(t) >= 2:
        return float(t[1])
    raise ValueError(f"Unrecognized tick format: {t!r}")


def _tick_epoch(t: Any) -> Optional[float]:
    if isinstance(t, dict):
        e = t.get("epoch")
        return float(e) if e is not None else None
    e = getattr(t, "epoch", None)
    if e is not None:
        return float(e)
    if isinstance(t, (tuple, list)) and len(t) >= 1:
        try:
            return float(t[0])
        except (TypeError, ValueError):
            return None
    return None


def _last_digit(quote: float, decimals: int) -> int:
    scaled = int(round(quote * (10 ** decimals)))
    return abs(scaled) % 10


def _digit_decimals(symbol: str) -> int:
    # config.DIGIT_DECIMALS: Optional[Dict[str, int]] — per-symbol pip
    # precision for last-digit extraction. Not present in the supplied
    # config.py; defaults to 2 decimals for every symbol until you add it.
    return getattr(config, "DIGIT_DECIMALS", {}).get(symbol, 2)


def _chi2_binary(count_a: int, count_b: int) -> Tuple[float, float]:
    """
    Chi-square goodness-of-fit for a 2-category 50/50 null (df=1).
    Returns (chi2_statistic, p_value). For df=1, chi2 is exactly the
    square of a standard normal variate, so the exact p-value is
    erfc(sqrt(chi2/2)) — no scipy dependency required.
    """
    n = count_a + count_b
    if n == 0:
        return 0.0, 1.0
    expected = n / 2.0
    chi2 = ((count_a - expected) ** 2) / expected + ((count_b - expected) ** 2) / expected
    p = math.erfc(math.sqrt(chi2 / 2.0))
    return chi2, p


# ---------------------------------------------------------------------------
# Strategy 1 — Digit Over/Under
# ---------------------------------------------------------------------------

def _digit_hybrid_check(
    ticks: Optional[List[Any]], symbol: str, digit_dir: str
) -> Tuple[bool, Optional[float], Optional[float], Optional[str]]:
    """
    Chi-square frequency-bias check on the same over/under threshold the
    indicator read used. Returns (agree, chi2, p_value, freq_biased).
    agree is False (never fires) if ticks are missing/insufficient —
    hybrid mode is a confirmation gate, not a fallback signal source.
    """
    if not ticks:
        return False, None, None, None

    window_n = getattr(config, "DIGIT_PARITY_WINDOW", 1000)
    min_n = getattr(config, "DIGIT_PARITY_MIN_SAMPLE", 500)
    alpha = getattr(config, "DIGIT_PARITY_ALPHA", 0.05)
    threshold = getattr(config, "DIGIT_OU_THRESHOLD", 5)

    window = ticks[-window_n:]
    n = len(window)
    if n < min_n:
        return False, None, None, None

    decimals = _digit_decimals(symbol)
    try:
        digits = [_last_digit(_tick_quote(t), decimals) for t in window]
    except ValueError:
        logger.warning(f"DIGIT hybrid: {symbol} could not parse tick quotes — skipping")
        return False, None, None, None

    over = sum(1 for d in digits if d > threshold)
    under = n - over
    chi2, p = _chi2_binary(over, under)
    freq_biased = "OVER" if over > under else "UNDER"
    agree = (freq_biased == digit_dir) and (p < alpha)
    return agree, chi2, p, freq_biased


def evaluate_digit(
    ltf_bars: List[Candle], symbol: str, ticks: Optional[List[Any]] = None
) -> SignalResult:
    if len(ltf_bars) < 25:
        return NONE_RESULT

    C, H, L = _arrays(ltf_bars)
    rsi = ind.rsi(C, 14)
    upper, mid, lower = ind.bollinger_bands(C, 20, 2.0)
    roc = ind.roc(C, 10)

    raw_score, digit_dir = ind.digit_score(
        closes=C, rsi_vals=rsi, bb_upper=upper, bb_lower=lower, roc_vals=roc
    )
    partial_score = raw_score / 8.0

    if digit_dir == "NONE" or raw_score < 6:
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

    # --- Hybrid confirmation gate (item 2) -------------------------------
    # Standalone behavior (flag False) is completely unchanged above this
    # point. When True, the indicator-based read above must additionally
    # agree with a chi-square frequency-bias read on the same threshold.
    if getattr(config, "DIGIT_HYBRID_MODE", False):
        agree, chi2, p, freq_biased = _digit_hybrid_check(ticks, symbol, digit_dir)
        if not agree:
            chi2_s = f"{chi2:.3f}" if chi2 is not None else "n/a"
            p_s = f"{p:.5f}" if p is not None else "n/a"
            logger.info(
                f"REJECTED: {symbol} DIGIT strength=0 score={score:.3f} — hybrid disagreement "
                f"(indicator={digit_dir}, freq={freq_biased}, chi2={chi2_s}, p={p_s})"
            )
            return SignalResult(
                "NONE", 0, score, "DIGIT",
                f"Hybrid mode: indicator/frequency disagreement (indicator={digit_dir}, freq={freq_biased})",
            )
        logger.info(
            f"DIGIT HYBRID: {symbol} indicator={digit_dir} freq={freq_biased} "
            f"chi2={chi2:.3f} p={p:.5f} — agree"
        )

    logger.info(f"DIGIT: {symbol} {direction} score={raw_score}/8")
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy="DIGIT",
        reason=f"Digit {digit_dir} raw={raw_score}/8",
    )


# ---------------------------------------------------------------------------
# Strategy 1b — Digit Parity (new, standalone)
# ---------------------------------------------------------------------------

def evaluate_digit_parity(ticks: Optional[List[Any]], symbol: str) -> SignalResult:
    window_n = getattr(config, "DIGIT_PARITY_WINDOW", 1000)
    min_n = getattr(config, "DIGIT_PARITY_MIN_SAMPLE", 500)
    alpha = getattr(config, "DIGIT_PARITY_ALPHA", 0.05)

    if not ticks:
        return NONE_RESULT

    window = ticks[-window_n:]
    n = len(window)
    if n < min_n:
        logger.debug(
            f"REJECTED: {symbol} DIGIT_PARITY strength=0 score=0.000 — "
            f"sample size {n} < {min_n}"
        )
        return NONE_RESULT

    decimals = _digit_decimals(symbol)
    try:
        digits = [_last_digit(_tick_quote(t), decimals) for t in window]
    except ValueError:
        logger.warning(f"DIGIT_PARITY: {symbol} could not parse tick quotes — skipping")
        return NONE_RESULT

    even_count = sum(1 for d in digits if d % 2 == 0)
    odd_count = n - even_count
    chi2, p = _chi2_binary(even_count, odd_count)

    logger.info(
        f"DIGIT_PARITY: {symbol} chi2={chi2:.3f} p={p:.5f} n={n} "
        f"even={even_count} odd={odd_count}"
    )

    if p >= alpha:
        logger.debug(
            f"REJECTED: {symbol} DIGIT_PARITY strength=0 score={max(0.0, 1 - p):.3f} — "
            f"p={p:.5f} not significant"
        )
        return SignalResult("NONE", 0, max(0.0, 1 - p), "DIGIT_PARITY", f"Not significant (p={p:.5f})")

    # Convention mirrors evaluate_digit's OVER/UNDER -> LONG/SHORT mapping:
    # LONG encodes an EVEN bias, SHORT encodes an ODD bias. This is a
    # digit-parity read, not a price-direction read — see chat reply.
    biased = "EVEN" if even_count > odd_count else "ODD"
    direction = "LONG" if biased == "EVEN" else "SHORT"
    score = max(0.0, min(1.0, 1 - p))
    strength = 3 if p < 0.01 else 2

    logger.info(f"SIGNAL: {symbol} {direction} DIGIT_PARITY strength={strength} score={score:.3f}")
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy="DIGIT_PARITY",
        reason=f"Parity bias={biased} chi2={chi2:.3f} p={p:.5f} n={n}",
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
# Strategy 2b — Volatility/Step Multiplier family (Implementation Brief v4)
# ---------------------------------------------------------------------------
# Replaces MEAN_REV for config.VOL_MULTIPLIER_SYMBOLS (R_10-R_100, 1HZ10V-
# 1HZ100V, stpRNG) now that these 11 symbols trade via Multiplier contracts
# (MULTUP/MULTDOWN) instead of Rise/Fall. Synthetic volatility indices are
# close to a pure random walk — there's no durable, strong directional edge
# to lean on — so this needs to (a) only fire when there's a real, currently
# -forming regime (trend vs range) rather than trading every cycle, and
# (b) size risk in a way that survives being wrong most of the time (see
# risk_manager.compute_dynamic_stop_loss_pct()). This is a bigger lever on
# profit factor than the strategy's win rate alone.

def _vol_regime(ltf_bars: List[Candle]) -> str:
    """
    'TREND' or 'RANGE', from EMA(8)/EMA(21) separation normalized by
    ATR(14). Cheap proxy for trend strength (ADX-equivalent) using only
    indicators already in indicators.py — no new dependency.
    """
    C, H, L = _arrays(ltf_bars)
    ema_fast = ind.ema(C, config.EMA_FAST)
    ema_slow = ind.ema(C, config.EMA_SLOW)
    atr = ind.atr(H, L, C, config.ATR_PERIOD)
    last_atr = _last(atr) or 1e-9
    sep = abs(_last(ema_fast) - _last(ema_slow))
    ratio = sep / last_atr
    return "TREND" if ratio >= getattr(config, "VOL_REGIME_TREND_RATIO", 0.6) else "RANGE"


def evaluate_vol_breakout(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    """
    Donchian-channel breakout + EMA trend alignment + MACD histogram
    confirmation. Suited to open-ended Multiplier risk (rides continuation
    rather than betting on a single-candle direction like Rise/Fall did).
    Fires in TREND regime — see evaluate_vol_regime() dispatcher below.
    """
    if len(ltf_bars) < 30:
        return NONE_RESULT

    C, H, L = _arrays(ltf_bars)
    ema_fast = ind.ema(C, config.EMA_FAST)
    ema_slow = ind.ema(C, config.EMA_SLOW)
    macd_line, signal_line, hist = ind.macd(C)
    # ind.donchian() — confirmed name/signature elsewhere in this file
    # (evaluate_step() uses `ind.donchian(H, L, 20)` → (upper, lower)).
    upper, lower = ind.donchian(H, L, 20)
    last_close = float(C[-1])

    long_score, short_score = 0, 0
    if last_close >= _last(upper):
        long_score += 3
    if last_close <= _last(lower):
        short_score += 3
    if _last(ema_fast) > _last(ema_slow):
        long_score += 2
    else:
        short_score += 2
    if _last(hist) > 0:
        long_score += 2
    elif _last(hist) < 0:
        short_score += 2

    if long_score >= 5 and long_score >= short_score:
        direction, raw = "LONG", long_score
    elif short_score >= 5:
        direction, raw = "SHORT", short_score
    else:
        best = max(long_score, short_score)
        logger.debug(f"REJECTED: {symbol} VOL_BREAKOUT strength=0 score={best/7.0:.3f} — no confirmed breakout")
        return SignalResult("NONE", 0, best / 7.0, "VOL_BREAKOUT", "No confirmed breakout")

    score = raw / 7.0
    strength = 3 if raw >= 6 else 2
    logger.info(
        f"SIGNAL: {symbol} {direction} VOL_BREAKOUT strength={strength} score={score:.3f}"
    )
    return SignalResult(
        direction=direction, strength=strength, score=score,
        strategy="VOL_BREAKOUT",
        reason=f"Donchian breakout, EMA-aligned, MACD-hist={_last(hist):.5f}",
    )


def evaluate_vol_reversion_mult(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    """
    Fires in RANGE regime — see evaluate_vol_regime() dispatcher below.
    Same confluence logic as evaluate_mean_reversion() (RSI extremes +
    Bollinger touch + ROC), kept as a *separate* named strategy
    (VOL_REV_MULT) so strategy_stats.py / meta_labeling.py don't conflate
    its historical performance with the old Rise/Fall version — the payoff
    structure is now completely different (open-ended + stop/target vs
    fixed 6-14min expiry), so the old win-rate history doesn't transfer.
    """
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
        logger.debug(f"REJECTED: {symbol} VOL_REV_MULT strength=0 score={best/8.0:.3f} — below threshold")
        return SignalResult("NONE", 0, best / 8.0, "VOL_REV_MULT", "Below entry threshold")

    score = raw / 8.0
    strength = 3 if all_met else (2 if score >= 6 / 8.0 else 1)
    if strength <= 1:
        logger.info(f"REJECTED: {symbol} VOL_REV_MULT strength=1 score={score:.3f} — below threshold")
        return SignalResult("NONE", 0, score, "VOL_REV_MULT", "Below entry threshold")

    logger.info(
        f"SIGNAL: {symbol} {direction} VOL_REV_MULT strength={strength} score={score:.3f}"
    )
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy="VOL_REV_MULT",
        # NOTE: deliberately does NOT carry over MEAN_REV's old "(70.8%
        # documented win rate)" claim — that number was measured on
        # Rise/Fall payoffs and does not apply to this Multiplier-contract
        # strategy. See strategy_stats.is_underperforming() for live
        # tracking instead.
        reason=f"VolRevMult RSI={last_rsi:.1f} raw={raw}/8",
    )


def evaluate_vol_regime(ltf_bars: List[Candle], symbol: str) -> SignalResult:
    """
    Thin regime-selecting dispatcher for config.VOL_MULTIPLIER_SYMBOLS —
    routes to the trend/breakout evaluator in TREND regime, or the
    range/reversion evaluator in RANGE regime.
    """
    if len(ltf_bars) < 30:
        return NONE_RESULT
    regime = _vol_regime(ltf_bars)
    if regime == "TREND":
        return evaluate_vol_breakout(ltf_bars, symbol)
    return evaluate_vol_reversion_mult(ltf_bars, symbol)


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

    # detect_spike() only reports on the single most-recent bar of whatever
    # slice it's given (+1 up-spike / -1 down-spike / 0 none — no dict, no
    # bars_ago/type/size). To ask "was the bar N bars ago a spike bar",
    # trim the array's tail by N bars so that bar becomes the new "last" one.
    def _spike_at(bars_ago: int, period: int = 14, atr_multiplier: float = 3.0):
        c_s = C[:-bars_ago] if bars_ago > 0 else C
        h_s = H[:-bars_ago] if bars_ago > 0 else H
        l_s = L[:-bars_ago] if bars_ago > 0 else L
        if len(c_s) < 2:
            return 0, 0.0
        direction = ind.detect_spike(c_s, h_s, l_s, period=period, atr_multiplier=atr_multiplier)
        size = abs(float(c_s[-1]) - float(c_s[-2])) if direction != 0 else 0.0
        return direction, size

    # Must be within last 2 bars, but at least 1 bar since the spike bar
    # closed (bars_ago=0 would be the still-forming most-recent bar).
    spike_dir, spike_size, bars_ago = 0, 0.0, 0
    for candidate in (1, 2):
        d, sz = _spike_at(candidate)
        if d != 0:
            spike_dir, spike_size, bars_ago = d, sz, candidate
            break

    if spike_dir == 0:
        logger.debug(f"REJECTED: {symbol} BOOM_CRASH strength=0 score=0.000 — below threshold")
        return NONE_RESULT

    spike_type = "BOOM" if spike_dir > 0 else "CRASH"

    # Cooldown: no earlier spike in the 10 bars preceding the one just found.
    cooldown_hit = False
    for earlier_bars_ago in range(bars_ago + 1, bars_ago + 11):
        earlier_dir, _ = _spike_at(earlier_bars_ago)
        if earlier_dir != 0:
            cooldown_hit = True
            break

    if cooldown_hit:
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
# Strategy 4b — Drift Fade (new, standalone, tick-based)
#
# Despite the name (kept as specified), this trades WITH a confirmed
# directional drift once a prior spike has cleared cooldown — it does not
# fade price the way evaluate_boom_crash does. It is a separate, distinct
# read on the same instrument category and is independently scored.
# ---------------------------------------------------------------------------

def evaluate_drift_fade(ticks: Optional[List[Any]], symbol: str) -> SignalResult:
    window_size = getattr(config, "DRIFT_FADE_WINDOW", 60)
    if not ticks or len(ticks) < window_size + 1:
        return NONE_RESULT

    try:
        quotes = np.array([_tick_quote(t) for t in ticks[-(window_size + 1):]], dtype=float)
    except ValueError:
        logger.warning(f"DRIFT_FADE: {symbol} could not parse tick quotes — skipping")
        return NONE_RESULT

    diffs = np.diff(quotes)
    atr_proxy = float(np.mean(np.abs(diffs))) or 1e-9

    spike_lookback = getattr(config, "DRIFT_FADE_SPIKE_LOOKBACK", 20)
    spike_mult = getattr(config, "DRIFT_FADE_SPIKE_MULT", 4.0)
    cooldown_ticks = getattr(config, "DRIFT_FADE_COOLDOWN_TICKS", 40)

    recent_diffs = diffs[-spike_lookback:]
    spike_idx = None
    for i in range(len(recent_diffs) - 1, -1, -1):
        if abs(recent_diffs[i]) > spike_mult * atr_proxy:
            spike_idx = i
            break

    if spike_idx is not None:
        ticks_since_spike = len(recent_diffs) - 1 - spike_idx
        if ticks_since_spike < cooldown_ticks:
            logger.debug(
                f"REJECTED: {symbol} DRIFT_FADE strength=0 score=0.000 — "
                f"cooldown active ({ticks_since_spike}/{cooldown_ticks} ticks since spike)"
            )
            return NONE_RESULT

    x = np.arange(len(quotes), dtype=float)
    slope = float(np.polyfit(x, quotes, 1)[0])
    slope_atr_ratio = abs(slope) / atr_proxy if atr_proxy else 0.0

    min_ratio = getattr(config, "DRIFT_FADE_MIN_SLOPE_ATR_RATIO", 0.15)
    if slope_atr_ratio < min_ratio:
        logger.debug(
            f"REJECTED: {symbol} DRIFT_FADE strength=0 score={min(slope_atr_ratio, 1.0):.3f} — "
            f"slope/ATR {slope_atr_ratio:.3f} below {min_ratio}"
        )
        return SignalResult("NONE", 0, min(slope_atr_ratio, 1.0), "DRIFT_FADE", "No confirmed drift")

    direction = "LONG" if slope > 0 else "SHORT"
    score = max(0.0, min(1.0, slope_atr_ratio))
    strength = 3 if slope_atr_ratio >= 2 * min_ratio else 2

    logger.info(
        f"SIGNAL: {symbol} {direction} DRIFT_FADE strength={strength} score={score:.3f} "
        f"slope={slope:.6f} atr_proxy={atr_proxy:.6f}"
    )
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy="DRIFT_FADE",
        reason=f"Confirmed drift slope={slope:.6f} atr_proxy={atr_proxy:.6f} ratio={slope_atr_ratio:.3f}",
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
# Strategy 6 — Jump Index Build-Up (new, standalone, tick-based)
# ---------------------------------------------------------------------------

def evaluate_jump_buildup(ticks: Optional[List[Any]], symbol: str) -> SignalResult:
    if not ticks or len(ticks) < 10:
        return NONE_RESULT

    try:
        quotes = np.array([_tick_quote(t) for t in ticks], dtype=float)
    except ValueError:
        logger.warning(f"JUMP_BUILDUP: {symbol} could not parse tick quotes — skipping")
        return NONE_RESULT

    epochs = [_tick_epoch(t) for t in ticks]
    have_epochs = all(e is not None for e in epochs)

    diffs = np.diff(quotes)
    baseline = float(np.median(np.abs(diffs))) or 1e-9
    jump_mult = getattr(config, "JUMP_DETECT_MULT", 5.0)

    jump_pos = None
    for i in range(len(diffs) - 1, -1, -1):
        if abs(diffs[i]) > jump_mult * baseline:
            jump_pos = i
            break

    if jump_pos is None:
        logger.debug(f"REJECTED: {symbol} JUMP_BUILDUP strength=0 score=0.000 — no jump detected in window")
        return NONE_RESULT

    target = getattr(config, "JUMP_TARGET_INTERVAL_MINS", 20)
    if have_epochs:
        elapsed_mins = (epochs[-1] - epochs[jump_pos + 1]) / 60.0
    else:
        # No timestamps on the tick objects — degrade to a tick-count proxy.
        # This is coarse (1 tick != 1 minute); wire timestamped ticks through
        # for accurate build-up timing.
        elapsed_mins = float(len(quotes) - 1 - jump_pos)
        logger.debug(f"JUMP_BUILDUP: {symbol} ticks have no epoch — using tick-count proxy for elapsed time")

    confidence = min(elapsed_mins / target, 1.0) if target > 0 else 0.0

    compression_lookback = getattr(config, "JUMP_COMPRESSION_LOOKBACK", 30)
    recent_window = diffs[-compression_lookback:] if len(diffs) >= compression_lookback else diffs
    recent_vol = float(np.std(recent_window))
    baseline_vol = float(np.std(diffs)) or 1e-9
    compressed = recent_vol < 0.7 * baseline_vol
    if compressed:
        confidence = min(1.0, confidence + 0.1)

    min_conf = getattr(config, "JUMP_MIN_CONFIDENCE", 0.5)
    if confidence < min_conf:
        logger.debug(
            f"REJECTED: {symbol} JUMP_BUILDUP strength=0 score={confidence:.3f} — "
            f"confidence below {min_conf} (elapsed={elapsed_mins:.1f}m, target={target}m)"
        )
        return SignalResult("NONE", 0, confidence, "JUMP_BUILDUP", "Build-up confidence too low")

    # Implementation Brief v3, finding #3 / task 2: build-up confidence has
    # no LONG/SHORT price read — jump direction is 50/50 by design (Deriv's
    # own product description), so there is nothing here to map onto a
    # Rise/Fall CALL/PUT. What build-up confidence DOES predict is whether
    # the last digit is likely to repeat (high confidence + compressed
    # pre-jump volatility -> MATCHES) or not (DIFFERS) — a real digit
    # contract, wired below via contract_kind="DIGIT" so bot_engine routes
    # it to DerivClient.buy_digit_contract() instead of buy_contract().
    # A digit barrier is required by the API for both MATCH and DIFFER, so
    # last_digit is computed unconditionally, not just on the MATCHES path.
    match_threshold = getattr(config, "JUMP_MATCH_CONFIDENCE_THRESHOLD", 0.9)
    decimals = _digit_decimals(symbol)
    last_digit = _last_digit(float(quotes[-1]), decimals)
    if confidence >= match_threshold and compressed:
        match_type = "MATCH"
        strength = 3
    else:
        match_type = "DIFFER"
        strength = 3 if confidence >= match_threshold else 2

    logger.info(
        f"SIGNAL: {symbol} JUMP_BUILDUP {match_type} digit={last_digit} "
        f"strength={strength} score={confidence:.3f} elapsed={elapsed_mins:.1f}m "
        f"target={target}m compressed={compressed}"
    )
    return SignalResult(
        direction=match_type,
        strength=strength,
        score=confidence,
        strategy="JUMP_BUILDUP",
        reason=(
            f"Recommend {match_type} digit={last_digit} | "
            f"elapsed={elapsed_mins:.1f}m/{target}m compressed={compressed}"
        ),
        contract_kind="DIGIT",
        digit=last_digit,
        match_type=match_type,
    )


# ---------------------------------------------------------------------------
# Strategy 7 — Bear/Bull ("Daily Reset") fixed-bias trend following
#
# Implementation Brief v3, finding #4: RDBEAR/RDBULL reset to a baseline at
# 00:00 GMT and then hold ONE fixed characteristic trend for the rest of the
# 24h cycle (Bull trends up, Bear trends down) — this is a fixed identity
# per symbol, not something that flips. The previous version alternated
# direction on every post-reset window via module-level state
# (_trend_shift_state), which contradicted that product mechanic outright
# (it would periodically have RDBULL go SHORT and RDBEAR go LONG). Fixed
# here: direction comes from config.BEAR_BULL_DIRECTION, a static map, and
# is never derived from EMA alignment or alternated — no more module-level
# direction state needed at all. is_post_reset()/get_bear_bull_state() is
# used ONLY to gate entry timing (skip trading during the post-reset window,
# since early-cycle behavior may differ from the rest of the trending cycle)
# via the shared `_symbol_manager` instance above, instead of duplicating
# the "minutes since 00:00 GMT" math locally.
# ---------------------------------------------------------------------------

def evaluate_trend_shift(
    ltf_bars: List[Candle],
    symbol: str,
    ticks: Optional[List[Any]] = None,
    is_post_reset_fn: Optional[Callable[[str], bool]] = None,
) -> SignalResult:
    """
    Bear/Bull fixed-bias trend following: direction is a static per-symbol
    fact (config.BEAR_BULL_DIRECTION), never derived from indicators and
    never alternated. EMA/RSI/ATR are computed only to score how cleanly
    current price action is confirming the known bias (a confidence read),
    and to gate out the post-reset window on timing rather than direction.

    ASSUMPTION: config.LTF_BARS is currently 30, which is fewer bars than
    EMA_TREND=50 needs to fully warm up. Depending on how indicators.ema()
    handles insufficient history (NaN-pad vs. shorter valid series vs.
    raising), this evaluator may run below full confidence — or never
    fire — until whatever calls SignalEngine.evaluate() is passing more
    than LTF_BARS=30 bars for BEAR_BULL_SYMBOLS specifically, or LTF_BARS
    is raised. Flagging rather than silently reinterpreting LTF_BARS.

    `ticks` is accepted only for call-site/signature compatibility with
    SignalEngine.evaluate()'s existing `ticks=ticks` call; unused here.

    `is_post_reset_fn` defaults to the shared `_symbol_manager.is_post_reset`
    (single source of truth); callers may still inject their own for
    testing.
    """
    bias_map = getattr(config, "BEAR_BULL_DIRECTION", {"RDBULL": "LONG", "RDBEAR": "SHORT"})
    direction = bias_map.get(symbol)
    if direction is None:
        return NONE_RESULT

    post_reset_fn = is_post_reset_fn or _symbol_manager.is_post_reset
    post_reset = post_reset_fn(symbol)
    if post_reset:
        window = getattr(config, "BEAR_BULL_TREND_SHIFT_MINS", 20)
        logger.info(
            f"REJECTED: {symbol} TREND_SHIFT strength=0 score=0.000 — "
            f"inside post-reset window ({window}min since 00:00 GMT), "
            f"waiting for it to close before sizing up"
        )
        return SignalResult(
            "NONE", 0, 0.0, "TREND_SHIFT",
            f"Post-reset window open ({window}min) — entry timing gate, bias={direction} unaffected",
        )

    min_bars = max(config.EMA_TREND, config.RSI_PERIOD, config.ATR_PERIOD) + 1
    if len(ltf_bars) < min_bars:
        return NONE_RESULT

    C, H, L = _arrays(ltf_bars)
    ema_fast_arr = ind.ema(C, config.EMA_FAST)
    ema_slow_arr = ind.ema(C, config.EMA_SLOW)
    ema_trend_arr = ind.ema(C, config.EMA_TREND)
    rsi_arr = ind.rsi(C, config.RSI_PERIOD)
    atr_arr = ind.atr(H, L, C, config.ATR_PERIOD)

    ema_fast = _last(ema_fast_arr)
    ema_slow = _last(ema_slow_arr)
    ema_trend = _last(ema_trend_arr)
    last_rsi = _last(rsi_arr)
    last_atr = _last(atr_arr)

    if any(math.isnan(v) for v in (ema_fast, ema_slow, ema_trend, last_rsi, last_atr)) or last_atr <= 0:
        logger.debug(f"REJECTED: {symbol} TREND_SHIFT strength=0 score=0.000 — indicators not warmed up")
        return NONE_RESULT

    # Confirmation check — does current EMA alignment support the KNOWN
    # fixed bias right now? This only scores conviction; it never changes
    # `direction`, which stays whatever bias_map says regardless.
    if direction == "LONG":
        aligned = ema_fast > ema_slow > ema_trend
    else:
        aligned = ema_fast < ema_slow < ema_trend

    separation_atr = abs(ema_fast - ema_slow) / last_atr
    raw_score = min(separation_atr / 3.0, 1.0)  # 3x ATR separation -> full score; tune with live data
    if not aligned:
        # The daily trend is supposed to hold all cycle by product design,
        # so a misaligned EMA read is more likely short-term noise than a
        # genuine reversal — dampen the score rather than reject outright.
        raw_score *= 0.5

    rsi_overbought = getattr(config, "RSI_OVERBOUGHT", 70)
    rsi_oversold = getattr(config, "RSI_OVERSOLD", 30)
    rsi_contradicts = (direction == "LONG" and last_rsi >= rsi_overbought) or (
        direction == "SHORT" and last_rsi <= rsi_oversold
    )
    score = raw_score * 0.6 if rsi_contradicts else raw_score
    score = max(0.0, min(1.0, score))

    min_score = getattr(config, "MIN_TREND_SHIFT_SCORE", 0.65)
    if score < min_score:
        logger.info(
            f"REJECTED: {symbol} TREND_SHIFT strength=0 score={score:.3f} — "
            f"below MIN_TREND_SHIFT_SCORE={min_score}"
        )
        return SignalResult("NONE", 0, score, "TREND_SHIFT", f"Score {score:.3f} below {min_score}")

    strength = 3 if score >= 0.85 else 2  # gated above min_score, so never falls to 1 here

    logger.info(
        f"SIGNAL: {symbol} {direction} TREND_SHIFT strength={strength} score={score:.3f} "
        f"fixed_bias=True aligned={aligned} ema_fast={ema_fast:.5f} ema_slow={ema_slow:.5f} "
        f"ema_trend={ema_trend:.5f} rsi={last_rsi:.1f} atr={last_atr:.5f} rsi_contradicts={rsi_contradicts}"
    )
    return SignalResult(
        direction=direction,
        strength=strength,
        score=score,
        strategy="TREND_SHIFT",
        reason=(
            f"Fixed daily-reset bias={direction} (not alternated), aligned={aligned}, "
            f"ema_sep/atr={separation_atr:.3f}, rsi={last_rsi:.1f}"
        ),
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SignalEngine:

    def __init__(self, *args, **kwargs):
        pass

    def evaluate(self, ltf_bars: List[Candle], symbol: str, **kwargs) -> SignalResult:
        ticks = kwargs.get("ticks")

        if symbol in getattr(config, "VOL_MULTIPLIER_SYMBOLS", []):
            result = evaluate_vol_regime(ltf_bars, symbol)
        elif symbol in config.DIGIT_SYMBOLS:
            result = evaluate_digit(ltf_bars, symbol, ticks=ticks)
        elif symbol in config.MEAN_REVERSION_SYMBOLS:
            result = evaluate_mean_reversion(ltf_bars, symbol)
        elif symbol in config.RANGE_BREAK_SYMBOLS:
            result = evaluate_range_break(ltf_bars, symbol)
        elif symbol in config.BOOM_CRASH_SYMBOLS:
            result = evaluate_boom_crash(ltf_bars, symbol)
        elif symbol in config.STEP_SYMBOLS:
            result = evaluate_step(ltf_bars, symbol)
        elif symbol in getattr(config, "DIGIT_PARITY_SYMBOLS", []):
            result = evaluate_digit_parity(ticks, symbol)
        elif symbol in getattr(config, "DRIFT_FADE_SYMBOLS", []):
            result = evaluate_drift_fade(ticks, symbol)
        elif symbol in getattr(config, "JUMP_BUILDUP_SYMBOLS", []):
            result = evaluate_jump_buildup(ticks, symbol)
        elif symbol in getattr(config, "BEAR_BULL_SYMBOLS", []):
            result = evaluate_trend_shift(ltf_bars, symbol, ticks=ticks)
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

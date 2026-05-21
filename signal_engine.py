"""
signal_engine.py – Signal Engine for the Deriv Trading Bot.
v16 — Two proven strategies only. All legacy SMC/EMA/MACD logic removed.

STRATEGY 1 — Range Break Breakout (RDBULL, RDBEAR):
  Consolidation: 15-bar range < 0.4 × 50-bar average range.
  Breakout: latest close > consolidation high + 0.3×ATR14 → LONG
            latest close < consolidation low  - 0.3×ATR14 → SHORT
  RSI14 confirmation: >52 for LONG, <48 for SHORT.
  Score: (breakout_size / ATR14) capped at 1.0. Minimum score: 0.3.
  Fresh breakouts only: breakout must have started <= 3 bars ago.
  Strength 3: consolidation + breakout + RSI → always emit.
  Strength 2: consolidation + breakout, RSI neutral → emit.
  Strength 1: breakout only (no consolidation) → reject.

STRATEGY 2 — Boom/Crash Post-Spike Fade (BOOM500, BOOM1000, CRASH500, CRASH1000, BOOM300, CRASH300):
  BOOM spike: single bar moves up > 3×ATR14 → enter PUT (fade).
  CRASH spike: single bar moves down > 3×ATR14 → enter CALL (fade).
  Spike must be <= 2 bars old. No signal if spike > 2 bars ago.
  Cooldown: no entry if another spike in last 10 bars.
  Wait 1 bar after spike bar closes before entering.
  RSI14 confirmation: >60 after BOOM spike, <40 after CRASH spike.
  Strength 3: spike + RSI extreme → always emit.
  Strength 2: spike, RSI not yet extreme → emit.
  No spike → no signal whatsoever on Boom/Crash.

Emit any signal with strength >= 2. No secondary rejection gates.
"""

import time
import logging
import numpy as np
from dataclasses import dataclass
from typing import List, Optional

import indicators as ind
import config

logger = logging.getLogger(__name__)

_RANGE_BREAK_SET : frozenset = frozenset(config.RANGE_BREAK_SYMBOLS)
_BOOM_CRASH_SET  : frozenset = frozenset(config.BOOM_CRASH_SYMBOLS)

# ─── Candle shim ─────────────────────────────────────────────────────────────
# Accepts either CandlestickBuilder Candle objects or plain dicts.

def _o(bar) -> float:
    return float(bar["open"]  if isinstance(bar, dict) else bar.open)

def _h(bar) -> float:
    return float(bar["high"]  if isinstance(bar, dict) else bar.high)

def _l(bar) -> float:
    return float(bar["low"]   if isinstance(bar, dict) else bar.low)

def _c(bar) -> float:
    return float(bar["close"] if isinstance(bar, dict) else bar.close)


# ─── SignalResult ─────────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    symbol           : str
    direction        : str    # "LONG" | "SHORT" | "NONE"
    strength         : int    # 0–3
    score            : float  # 0.0–1.0 composite
    confidence       : int    # legacy field (= strength for ranking)
    bull_votes       : int    # legacy field
    bear_votes       : int    # legacy field
    timestamp        : float
    emitted          : bool
    rejection_reason : str = ""

    @property
    def reason(self) -> str:
        return self.rejection_reason

    # Legacy shims for bot_engine compatibility
    @property
    def m1_signal(self) -> int:
        return 0

    @property
    def m2_signal(self) -> int:
        return 0


def _none_result(symbol: str, reason: str, ts: float) -> SignalResult:
    return SignalResult(
        symbol=symbol, direction="NONE", strength=0, score=0.0,
        confidence=0, bull_votes=0, bear_votes=0,
        timestamp=ts, emitted=False, rejection_reason=reason)


# ─── Strategy 1: Range Break Breakout ─────────────────────────────────────────

def evaluate_range_break(ltf_bars: list, symbol: str) -> SignalResult:
    ts = time.time()

    MIN_BARS = 52   # need 50 for ATR + 2 buffer
    if len(ltf_bars) < MIN_BARS:
        return _none_result(symbol, f"insufficient bars ({len(ltf_bars)}<{MIN_BARS})", ts)

    try:
        bars  = ltf_bars[-52:]
        highs  = np.array([_h(b) for b in bars])
        lows   = np.array([_l(b) for b in bars])
        closes = np.array([_c(b) for b in bars])

        atr14 = ind.atr(highs, lows, closes, 14)
        atr_val = float(atr14[-1])
        if atr_val <= 0:
            return _none_result(symbol, "ATR=0 — no volatility data", ts)

        # 50-bar average range for reference
        ranges_50 = highs[-50:] - lows[-50:]
        avg_range_50 = float(np.mean(ranges_50))

        # Consolidation: last 15 bars
        consol_highs  = highs[-15:]
        consol_lows   = lows[-15:]
        consol_high   = float(np.max(consol_highs))
        consol_low    = float(np.min(consol_lows))
        consol_range  = consol_high - consol_low

        in_consolidation = (avg_range_50 > 0) and (consol_range < 0.4 * avg_range_50)

        latest_close = float(closes[-1])
        breakout_threshold = 0.3 * atr_val

        long_breakout  = latest_close > (consol_high + breakout_threshold)
        short_breakout = latest_close < (consol_low  - breakout_threshold)

        if not long_breakout and not short_breakout:
            return _none_result(symbol, "no breakout above consolidation±0.3×ATR", ts)

        direction = "LONG" if long_breakout else "SHORT"

        # Freshness check: breakout must have started <= 3 bars ago
        fresh = False
        for i in range(1, min(4, len(closes))):
            bar_close = float(closes[-i])
            if direction == "LONG"  and bar_close <= consol_high:
                fresh = True
                break
            if direction == "SHORT" and bar_close >= consol_low:
                fresh = True
                break
        if not fresh:
            return _none_result(symbol, "stale breakout (>3 bars ago)", ts)

        # Score: breakout size / ATR, capped at 1.0
        if direction == "LONG":
            breakout_size = latest_close - consol_high
        else:
            breakout_size = consol_low - latest_close
        score = min(1.0, breakout_size / atr_val)

        if score < 0.3:
            return _none_result(symbol, f"score {score:.3f} < 0.3 minimum", ts)

        # RSI confirmation
        rsi14   = ind.rsi(closes, 14)
        rsi_val = float(rsi14[-1])
        rsi_confirms = (direction == "LONG"  and rsi_val > 52) or \
                       (direction == "SHORT" and rsi_val < 48)

        # Strength logic
        if in_consolidation and rsi_confirms:
            strength = 3
        elif in_consolidation:
            strength = 2   # consolidation + breakout, RSI neutral
        else:
            # Breakout only, no consolidation — reject
            return _none_result(symbol,
                f"breakout without consolidation (range={consol_range:.5f} avg={avg_range_50:.5f})", ts)

        logger.info(
            f"RANGE BREAK {symbol} {direction} | strength={strength} score={score:.3f} "
            f"rsi={rsi_val:.1f} consolidation={in_consolidation} fresh={fresh}")

        bull_v = 1 if direction == "LONG"  else 0
        bear_v = 1 if direction == "SHORT" else 0

        return SignalResult(
            symbol=symbol, direction=direction, strength=strength, score=round(score, 4),
            confidence=strength, bull_votes=bull_v, bear_votes=bear_v,
            timestamp=ts, emitted=True, rejection_reason="")

    except Exception as exc:
        logger.exception(f"evaluate_range_break error on {symbol}: {exc}")
        return _none_result(symbol, f"exception: {exc}", ts)


# ─── Strategy 2: Boom/Crash Post-Spike Fade ───────────────────────────────────

def evaluate_boom_crash(ltf_bars: list, symbol: str) -> SignalResult:
    ts = time.time()

    MIN_BARS = 20
    if len(ltf_bars) < MIN_BARS:
        return _none_result(symbol, f"insufficient bars ({len(ltf_bars)}<{MIN_BARS})", ts)

    try:
        bars   = ltf_bars[-max(52, len(ltf_bars)):]
        highs  = np.array([_h(b) for b in bars])
        lows   = np.array([_l(b) for b in bars])
        closes = np.array([_c(b) for b in bars])

        atr14   = ind.atr(highs, lows, closes, 14)
        atr_val = float(atr14[-1])
        if atr_val <= 0:
            return _none_result(symbol, "ATR=0 — no volatility data", ts)

        spike_threshold = config.SPIKE_ATR_MULTIPLIER * atr_val  # 3×ATR
        max_age         = config.SPIKE_MAX_AGE_BARS              # 2
        cooldown        = config.SPIKE_COOLDOWN_BARS             # 10

        # Detect all spikes in last (cooldown+max_age) bars for cooldown check
        window_size = min(cooldown + max_age + 1, len(closes))
        recent_closes = closes[-window_size:]

        # Check cooldown: any spike in last `cooldown` bars (excluding the 2 most recent)
        cooldown_zone = recent_closes[:-max_age] if len(recent_closes) > max_age else recent_closes
        cooldown_spike = False
        if len(cooldown_zone) >= 2:
            for i in range(1, len(cooldown_zone)):
                move = abs(float(cooldown_zone[i]) - float(cooldown_zone[i - 1]))
                if move > spike_threshold:
                    cooldown_spike = True
                    break

        if cooldown_spike:
            return _none_result(symbol, f"spike cooldown active (spike in last {cooldown} bars)", ts)

        # Look for fresh spike in last `max_age` bars (bar[-2] and bar[-1])
        # We require waiting 1 bar after spike, so:
        #   spike at bar[-2]: we are now on bar[-1], safe to enter
        #   spike at bar[-1] (current): wait for next bar, skip now
        spike_detected  = False
        spike_direction = None   # "BOOM" or "CRASH"

        # Check bar[-2] (spike has closed, we wait 1 bar)
        if len(closes) >= 2:
            move = float(closes[-2]) - float(closes[-3]) if len(closes) >= 3 else 0.0
            if move > spike_threshold:
                spike_detected  = True
                spike_direction = "BOOM"
            elif move < -spike_threshold:
                spike_detected  = True
                spike_direction = "CRASH"

        if not spike_detected:
            return _none_result(symbol, "no fresh spike within last 2 bars", ts)

        # Direction: after BOOM spike → PUT (SHORT), after CRASH spike → CALL (LONG)
        direction = "SHORT" if spike_direction == "BOOM" else "LONG"

        # RSI confirmation
        rsi14   = ind.rsi(closes, 14)
        rsi_val = float(rsi14[-1])

        if spike_direction == "BOOM":
            rsi_confirms = rsi_val > 60   # overbought after BOOM spike
        else:
            rsi_confirms = rsi_val < 40   # oversold after CRASH spike

        strength = 3 if rsi_confirms else 2
        score    = round(1.0 if rsi_confirms else 0.7, 4)

        logger.info(
            f"BOOM/CRASH {symbol} {direction} (fade {spike_direction} spike) | "
            f"strength={strength} rsi={rsi_val:.1f} rsi_confirms={rsi_confirms}")

        bull_v = 1 if direction == "LONG"  else 0
        bear_v = 1 if direction == "SHORT" else 0

        return SignalResult(
            symbol=symbol, direction=direction, strength=strength, score=score,
            confidence=strength, bull_votes=bull_v, bear_votes=bear_v,
            timestamp=ts, emitted=True, rejection_reason="")

    except Exception as exc:
        logger.exception(f"evaluate_boom_crash error on {symbol}: {exc}")
        return _none_result(symbol, f"exception: {exc}", ts)


# ─── SignalEngine ─────────────────────────────────────────────────────────────

class SignalEngine:
    """
    Routes each symbol to its strategy engine.
    Range Break → evaluate_range_break
    Boom/Crash  → evaluate_boom_crash
    Anything else → NONE (never traded)
    """

    def __init__(self, symbols: list = None, config=None, **kwargs):
        self.symbols = symbols or []
        self.config  = config

    def evaluate(
        self,
        ltf_bars : list,
        symbol   : str  = "",
        htf_bias : str  = "",   # ignored — not used on RNG synthetics
        smc_ctx  = None,        # ignored
        in_zone  : bool = True, # ignored
        **kwargs,
    ) -> SignalResult:
        sym = symbol or "UNKNOWN"

        if sym in _RANGE_BREAK_SET:
            result = evaluate_range_break(ltf_bars, sym)
        elif sym in _BOOM_CRASH_SET:
            result = evaluate_boom_crash(ltf_bars, sym)
        else:
            return _none_result(sym, "symbol not in TRADE_SYMBOLS — no signal generated", time.time())

        # Final strength gate (should never block str=3 per spec)
        if sym in _RANGE_BREAK_SET:
            min_strength = getattr(config, "MIN_STRENGTH_RANGE_BREAK", 2)
        else:
            min_strength = getattr(config, "MIN_STRENGTH_BOOM_CRASH", 2)

        if result.emitted and result.strength < min_strength:
            result.emitted          = False
            result.direction        = "NONE"
            result.rejection_reason = f"strength {result.strength} < min {min_strength}"
            logger.info(f"REJECTED: {sym} {result.rejection_reason}")

        return result

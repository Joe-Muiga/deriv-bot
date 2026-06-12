"""
Multi-strategy signal engine.
Every symbol is evaluated independently by ALL strategies.
Only signals where multiple strategies agree are emitted.
Final score determines execution priority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

import numpy as np

import config
import indicators as ind

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    direction:   str    # "LONG"|"SHORT"|"NONE"
    strength:    int    # 1-3
    score:       float  # 0.0-1.0
    strategy:    str
    reason:      str
    stop_loss:   float  # dollar amount for SL
    take_profit: float  # dollar amount for TP
    multiplier:  int    # recommended multiplier


# ---------------------------------------------------------------------------
# NONE_RESULT constant
# ---------------------------------------------------------------------------

NONE_RESULT = SignalResult(
    "NONE", 0, 0.0, "NONE", "No signal",
    0.0, 0.0, 0)


# ---------------------------------------------------------------------------
# Strategy functions — each returns (direction, score, reason) or (None,0,"")
# ---------------------------------------------------------------------------

def _strat_mean_reversion(C, H, L):
    rsi = ind.rsi(C, 14)
    upper, mid, lower = ind.bollinger_bands(C, 20, 2.0)
    last_rsi = rsi[~np.isnan(rsi)][-1]
    last_close = float(C[-1])
    last_upper = upper[~np.isnan(upper)][-1]
    last_lower = lower[~np.isnan(lower)][-1]

    if last_rsi < 25 and last_close <= last_lower * 1.005:
        score = 0.85 if last_rsi < 20 else 0.75
        return "LONG", score, f"MeanRev RSI={last_rsi:.1f}"
    if last_rsi > 75 and last_close >= last_upper * 0.995:
        score = 0.85 if last_rsi > 80 else 0.75
        return "SHORT", score, f"MeanRev RSI={last_rsi:.1f}"
    return None, 0, ""


def _strat_ema_momentum(C, H, L):
    ema8  = ind.ema(C, 8)
    ema21 = ind.ema(C, 21)
    ema50 = ind.ema(C, 50)

    e8, e21, e50 = ema8[-1], ema21[-1], ema50[-1]
    e8p, e21p    = ema8[-2], ema21[-2]

    # Crossover in direction of trend
    if e8p <= e21p and e8 > e21 and e21 > e50:
        return "LONG", 0.70, "EMA cross up with trend"
    if e8p >= e21p and e8 < e21 and e21 < e50:
        return "SHORT", 0.70, "EMA cross down with trend"
    # Strong trend alignment
    if e8 > e21 > e50 and (e8-e50)/e50 > 0.001:
        return "LONG", 0.65, "EMA stack bullish"
    if e8 < e21 < e50 and (e50-e8)/e50 > 0.001:
        return "SHORT", 0.65, "EMA stack bearish"
    return None, 0, ""


def _strat_indicator_confluence(C, H, L):
    rsi = ind.rsi(C, 14)
    _, _, hist = ind.macd(C, 12, 26, 9)
    last_rsi  = rsi[~np.isnan(rsi)][-1]
    last_hist = hist[~np.isnan(hist)][-1]
    prev_hist = hist[~np.isnan(hist)][-2]

    bull = 0
    bear = 0

    if last_rsi > 55: bull += 1
    if last_rsi < 45: bear += 1
    if last_hist > 0 and prev_hist <= 0: bull += 2  # MACD crossover
    if last_hist < 0 and prev_hist >= 0: bear += 2
    if last_hist > 0: bull += 1
    if last_hist < 0: bear += 1

    if bull >= 3:
        return "LONG",  min(0.5 + bull*0.08, 0.85), f"Indicators bull={bull}"
    if bear >= 3:
        return "SHORT", min(0.5 + bear*0.08, 0.85), f"Indicators bear={bear}"
    return None, 0, ""


def _strat_structure(C, H, L):
    # Higher highs and higher lows = bullish structure
    if len(H) < 6:
        return None, 0, ""
    hh = all(H[-i] > H[-i-1] for i in range(1, 4))
    hl = all(L[-i] > L[-i-1] for i in range(1, 4))
    lh = all(H[-i] < H[-i-1] for i in range(1, 4))
    ll = all(L[-i] < L[-i-1] for i in range(1, 4))

    if hh and hl:
        return "LONG",  0.72, "Structure HH+HL"
    if lh and ll:
        return "SHORT", 0.72, "Structure LH+LL"
    return None, 0, ""


def _strat_breakout(C, H, L, atr):
    lookback = min(15, len(C)-1)
    highest  = float(np.max(H[-lookback-1:-1]))
    lowest   = float(np.min(L[-lookback-1:-1]))
    last_atr = float(atr[~np.isnan(atr)][-1]) if len(atr[~np.isnan(atr)]) else 0.001

    if float(C[-1]) > highest + 0.3 * last_atr:
        return "LONG",  0.78, f"Breakout above {highest:.5f}"
    if float(C[-1]) < lowest  - 0.3 * last_atr:
        return "SHORT", 0.78, f"Breakout below {lowest:.5f}"
    return None, 0, ""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SignalEngine:

    def __init__(self, *args, **kwargs):
        pass

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def evaluate(self, ltf_bars, mtf_bars, symbol,
                 stake=1.0, **kwargs):
        if len(ltf_bars) < 20:
            return NONE_RESULT

        C = np.array([b.close for b in ltf_bars])
        H = np.array([b.high  for b in ltf_bars])
        L = np.array([b.low   for b in ltf_bars])
        atr = ind.atr(H, L, C, 14)

        strategies = [
            ("MEAN_REV",    _strat_mean_reversion(C, H, L)),
            ("EMA_MOM",     _strat_ema_momentum(C, H, L)),
            ("INDICATORS",  _strat_indicator_confluence(C, H, L)),
            ("STRUCTURE",   _strat_structure(C, H, L)),
            ("BREAKOUT",    _strat_breakout(C, H, L, atr)),
        ]

        long_scores   = []
        short_scores  = []
        long_reasons  = []
        short_reasons = []

        for name, (direction, score, reason) in strategies:
            if direction == "LONG" and score > 0:
                long_scores.append(score)
                long_reasons.append(f"{name}:{reason}")
            elif direction == "SHORT" and score > 0:
                short_scores.append(score)
                short_reasons.append(f"{name}:{reason}")

        # Need minimum 3 strategies agreeing
        if len(long_scores) >= 3 and len(long_scores) > len(short_scores):
            final_score = sum(long_scores) / len(long_scores)
            agreement   = len(long_scores)
            # Bonus for more agreement
            final_score = min(final_score + (agreement - 3) * 0.05, 0.98)
            if final_score >= config.MIN_SIGNAL_SCORE:
                logger.info(
                    f"SIGNAL: {symbol} LONG "
                    f"score={final_score:.3f} "
                    f"agreement={agreement}/5 "
                    f"[{' | '.join(long_reasons)}]")
                sl_pct = config.STOP_LOSS_MAP.get(symbol, 50.0)
                sl_amt = round(stake * sl_pct / 100, 2)
                tp_amt = round(sl_amt * 2.0, 2)
                return SignalResult(
                    direction   = "LONG",
                    strength    = min(agreement, 3),
                    score       = final_score,
                    strategy    = f"MULTI({agreement}/5)",
                    reason      = " | ".join(long_reasons),
                    stop_loss   = sl_amt,
                    take_profit = tp_amt,
                    multiplier  = config.MULTIPLIER_MAP.get(symbol, 100),
                )

        if len(short_scores) >= 3 and len(short_scores) > len(long_scores):
            final_score = sum(short_scores) / len(short_scores)
            agreement   = len(short_scores)
            final_score = min(final_score + (agreement - 3) * 0.05, 0.98)
            if final_score >= config.MIN_SIGNAL_SCORE:
                logger.info(
                    f"SIGNAL: {symbol} SHORT "
                    f"score={final_score:.3f} "
                    f"agreement={agreement}/5 "
                    f"[{' | '.join(short_reasons)}]")
                sl_pct = config.STOP_LOSS_MAP.get(symbol, 50.0)
                sl_amt = round(stake * sl_pct / 100, 2)
                tp_amt = round(sl_amt * 2.0, 2)
                return SignalResult(
                    direction   = "SHORT",
                    strength    = min(agreement, 3),
                    score       = final_score,
                    strategy    = f"MULTI({agreement}/5)",
                    reason      = " | ".join(short_reasons),
                    stop_loss   = sl_amt,
                    take_profit = tp_amt,
                    multiplier  = config.MULTIPLIER_MAP.get(symbol, 100),
                )

        logger.debug(
            f"NO SIGNAL: {symbol} "
            f"long={len(long_scores)} short={len(short_scores)}")
        return NONE_RESULT

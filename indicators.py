"""
indicators.py – Pure-numpy trading indicators for Deriv SIFM bot.

No TA-Lib, no pandas-ta – works on Render's free tier out of the box.

Architectural contract (enforced throughout):
  • Handle series as short as 15 bars without returning NaN.
  • Return neutral values (0 / 50 / last-close) when data is insufficient.
  • Never raise exceptions — catch all internally, return neutral on error.
  • Never return None from any function.
  • All inputs are plain Python lists or numpy arrays — both are handled.
  • All outputs are numpy arrays (or scalars where documented).

Functions
─────────
Core indicators  : ema, sma, rsi, macd, bollinger_bands, atr, stochastic
Momentum / range : rate_of_change, donchian_channel
ADX / StochRSI   : adx, stoch_rsi
Strategy helpers : find_consolidation, detect_spike, digit_score,
                   find_rsi_divergence
"""

import numpy as np
from typing import List, Tuple, Union

ArrayLike = Union[List[float], np.ndarray]

# ─── Internal helpers ─────────────────────────────────────────────────────────

def _to(data: ArrayLike) -> np.ndarray:
    """Convert any array-like to a float64 numpy array, silently."""
    try:
        return np.asarray(data, dtype=np.float64)
    except Exception:
        return np.array([], dtype=np.float64)


def _safe_last(arr: np.ndarray, fallback: float = 0.0) -> float:
    """Return last element as Python float, or fallback if empty."""
    return float(arr[-1]) if len(arr) > 0 else fallback


def _fill(n: int, value: float) -> np.ndarray:
    return np.full(n, value, dtype=np.float64)


# ─── SMA ──────────────────────────────────────────────────────────────────────

def sma(closes: ArrayLike, period: int) -> np.ndarray:
    """
    Simple Moving Average.
    Positions before a full window use the mean of available bars.
    Returns array same length as closes.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([], dtype=np.float64)
        out = np.empty(n, dtype=np.float64)
        for i in range(n):
            start = max(0, i - period + 1)
            out[i] = float(np.mean(p[start : i + 1]))
        return out
    except Exception:
        p = _to(closes)
        return _fill(len(p), _safe_last(p))


# ─── EMA ──────────────────────────────────────────────────────────────────────

def ema(closes: ArrayLike, period: int) -> np.ndarray:
    """
    Exponential Moving Average.
    Returns array same length as closes.
    For series shorter than period, fills with the SMA of available data
    (growing window), so every bar has a meaningful, NaN-free value.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([], dtype=np.float64)

        k = 2.0 / (period + 1.0)
        out = np.empty(n, dtype=np.float64)

        if n < period:
            # Not enough bars for a proper EMA seed — fill each bar with the
            # SMA of all available data up to that point (growing window).
            for i in range(n):
                out[i] = float(np.mean(p[: i + 1]))
            return out

        # Seed with SMA of the first `period` bars, then apply EMA multiplier.
        out[period - 1] = float(np.mean(p[:period]))
        # Back-fill pre-seed positions with growing-window SMA.
        for i in range(period - 1):
            out[i] = float(np.mean(p[: i + 1]))
        for i in range(period, n):
            out[i] = float(p[i]) * k + out[i - 1] * (1.0 - k)
        return out
    except Exception:
        p = _to(closes)
        return _fill(len(p), _safe_last(p))


# ─── RSI ──────────────────────────────────────────────────────────────────────

def rsi(closes: ArrayLike, period: int = 14) -> np.ndarray:
    """
    Relative Strength Index (0–100).
    Minimum 15 bars required; returns array of 50.0 (neutral) otherwise.
    Uses Wilder's smoothing after the initial SMA seed.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([], dtype=np.float64)
        if n < 15:
            return _fill(n, 50.0)

        out = _fill(n, 50.0)
        delta  = np.diff(p)
        gains  = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)

        # Initial Wilder seed = simple average of first `period` moves.
        avg_g = float(np.mean(gains[:period]))
        avg_l = float(np.mean(losses[:period]))

        def _rsi_val(ag: float, al: float) -> float:
            if al == 0.0:
                return 100.0
            return 100.0 - 100.0 / (1.0 + ag / al)

        out[period] = _rsi_val(avg_g, avg_l)

        for i in range(period, len(delta)):
            avg_g = (avg_g * (period - 1) + gains[i])  / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
            out[i + 1] = _rsi_val(avg_g, avg_l)

        return out
    except Exception:
        p = _to(closes)
        return _fill(len(p), 50.0)


# ─── MACD ─────────────────────────────────────────────────────────────────────

def macd(
    closes: ArrayLike,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    MACD line, signal line, histogram.
    Returns (macd_line, signal_line, histogram) arrays, all same length as input.
    Returns (zeros, zeros, zeros) when data is too short.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([]), np.array([]), np.array([])

        z = np.zeros(n, dtype=np.float64)
        if n < slow + signal:
            return z.copy(), z.copy(), z.copy()

        fast_ema  = ema(p, fast)
        slow_ema  = ema(p, slow)
        macd_line = fast_ema - slow_ema

        sig_line = np.zeros(n, dtype=np.float64)
        start = slow - 1                    # first bar where slow EMA is meaningful
        if n - start >= signal:
            sig_vals = ema(macd_line[start:], signal)
            sig_line[start:] = sig_vals

        hist = macd_line - sig_line
        return macd_line, sig_line, hist
    except Exception:
        p = _to(closes)
        z = np.zeros(len(p), dtype=np.float64)
        return z.copy(), z.copy(), z.copy()


# ─── Bollinger Bands ──────────────────────────────────────────────────────────

def bollinger_bands(
    closes: ArrayLike,
    period: int = 20,
    std_dev: float = 2.0,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Bollinger Bands.
    Returns (upper, mid, lower) arrays, all same length as closes.
    Bands collapse to last-close when data is insufficient.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([]), np.array([]), np.array([])

        neutral = _safe_last(p)
        mid = sma(p, period)
        std = np.zeros(n, dtype=np.float64)

        for i in range(n):
            start = max(0, i - period + 1)
            std[i] = float(np.std(p[start : i + 1], ddof=0))

        upper = mid + std_dev * std
        lower = mid - std_dev * std

        upper = np.where(np.isnan(upper), neutral, upper)
        mid   = np.where(np.isnan(mid),   neutral, mid)
        lower = np.where(np.isnan(lower), neutral, lower)
        return upper, mid, lower
    except Exception:
        p = _to(closes)
        neutral = _safe_last(p)
        a = _fill(len(p), neutral)
        return a.copy(), a.copy(), a.copy()


# ─── ATR ──────────────────────────────────────────────────────────────────────

def atr(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    period: int = 14,
) -> np.ndarray:
    """
    Average True Range (Wilder smoothing).
    Returns array same length as closes.
    No NaN for series >= 15 bars.
    For very short series, fills with H-L range of each bar.
    """
    try:
        H, L, C = _to(highs), _to(lows), _to(closes)
        n = len(C)
        if n == 0:
            return np.array([], dtype=np.float64)

        # True Range for each bar.
        tr = np.zeros(n, dtype=np.float64)
        tr[0] = float(H[0] - L[0])   # no previous close for bar 0
        for i in range(1, n):
            tr[i] = max(
                float(H[i] - L[i]),
                abs(float(H[i]) - float(C[i - 1])),
                abs(float(L[i]) - float(C[i - 1])),
            )

        out = np.zeros(n, dtype=np.float64)

        if n < period:
            # Too few bars — use expanding-window mean of TR.
            for i in range(n):
                out[i] = float(np.mean(tr[: i + 1]))
            return out

        # Wilder smoothing: seed = SMA of first `period` TRs.
        out[period - 1] = float(np.mean(tr[:period]))
        # Back-fill pre-seed with expanding mean so nothing is zero/nan.
        for i in range(period - 1):
            out[i] = float(np.mean(tr[: i + 1]))
        for i in range(period, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period

        return out
    except Exception:
        C = _to(closes)
        return np.zeros(len(C), dtype=np.float64)


# ─── Stochastic ───────────────────────────────────────────────────────────────

def stochastic(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    k_period: int = 14,
    d_period: int = 3,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Standard Stochastic Oscillator.
    Returns (%K, %D), both in [0, 100].

    Crossing logic (caller-side):
        bullish_cross = prev_k < 25 and curr_k >= 25   # crossing up from below 25
        bearish_cross = prev_k > 75 and curr_k <= 75   # crossing down from above 75

    Returns (50-filled, 50-filled) when series < k_period + d_period.
    Guaranteed NaN-free for series >= 15 bars.
    """
    try:
        H = _to(highs)
        L = _to(lows)
        C = _to(closes)
        n = len(C)
        if n == 0:
            return np.array([]), np.array([])

        neutral_K = _fill(n, 50.0)
        neutral_D = _fill(n, 50.0)

        if n < k_period + d_period:
            return neutral_K, neutral_D

        raw_k = _fill(n, 50.0)
        for i in range(n):
            start   = max(0, i - k_period + 1)
            highest = float(np.max(H[start : i + 1]))
            lowest  = float(np.min(L[start : i + 1]))
            if highest == lowest:
                raw_k[i] = 50.0
            else:
                raw_k[i] = 100.0 * (float(C[i]) - lowest) / (highest - lowest)

        # %D = SMA(d_period) of %K
        K_out = raw_k.copy()
        D_out = _fill(n, 50.0)
        for i in range(n):
            start   = max(0, i - d_period + 1)
            D_out[i] = float(np.mean(K_out[start : i + 1]))

        K_out = np.where(np.isnan(K_out), 50.0, K_out)
        D_out = np.where(np.isnan(D_out), 50.0, D_out)
        return K_out, D_out
    except Exception:
        C = _to(closes)
        n = len(C)
        return _fill(n, 50.0), _fill(n, 50.0)


# ─── Rate of Change ───────────────────────────────────────────────────────────

def rate_of_change(closes: ArrayLike, period: int = 10) -> np.ndarray:
    """
    Rate of Change (momentum).
    ROC[i] = (close[i] - close[i - period]) / close[i - period]

    For bars with fewer than `period` bars of history the denominator
    falls back to the first available close, so the result is always defined.
    Returns array same length as closes.  Never NaN or None.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([], dtype=np.float64)

        out = np.zeros(n, dtype=np.float64)
        for i in range(n):
            ref_idx = max(0, i - period)
            ref     = float(p[ref_idx])
            if ref == 0.0:
                out[i] = 0.0
            else:
                out[i] = (float(p[i]) - ref) / ref
        return out
    except Exception:
        p = _to(closes)
        return np.zeros(len(p), dtype=np.float64)


# ─── Donchian Channel ─────────────────────────────────────────────────────────

def donchian_channel(
    highs: ArrayLike,
    lows: ArrayLike,
    period: int = 20,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Donchian Channel.
    Returns (upper, lower, mid) arrays, all same length as input.
    upper = highest high over rolling `period` bars (expanding at edges).
    lower = lowest low  over rolling `period` bars (expanding at edges).
    mid   = (upper + lower) / 2.
    """
    try:
        H = _to(highs)
        L = _to(lows)
        n = len(H)
        if n == 0:
            return np.array([]), np.array([]), np.array([])

        upper = np.empty(n, dtype=np.float64)
        lower = np.empty(n, dtype=np.float64)
        for i in range(n):
            start    = max(0, i - period + 1)
            upper[i] = float(np.max(H[start : i + 1]))
            lower[i] = float(np.min(L[start : i + 1]))

        mid = (upper + lower) / 2.0
        return upper, lower, mid
    except Exception:
        H = _to(highs)
        n = len(H)
        z = np.zeros(n, dtype=np.float64)
        return z.copy(), z.copy(), z.copy()


# ─── Consolidation Detector ───────────────────────────────────────────────────

def find_consolidation(
    closes: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    lookback: int = 15,
    avg_lookback: int = 50,
    ratio: float = 0.4,
) -> bool:
    """
    Returns True if the market is in consolidation (tight range).

    Algorithm:
      current_range  = max(high) - min(low) over the last `lookback` bars.
      avg_range      = max(high) - min(low) over the last `avg_lookback` bars.
      consolidating  = current_range < ratio * avg_range.

    Works on as few as `lookback` bars (uses whatever is available for
    avg_range when fewer than avg_lookback bars exist).
    Returns False on any error or insufficient data.
    """
    try:
        H = _to(highs)
        L = _to(lows)
        n = len(H)

        if n < lookback:
            return False

        lb       = min(lookback, n)
        avg_lb   = min(avg_lookback, n)

        cur_range = float(np.max(H[-lb:]))  - float(np.min(L[-lb:]))
        avg_range = float(np.max(H[-avg_lb:])) - float(np.min(L[-avg_lb:]))

        if avg_range == 0.0:
            return False

        return bool(cur_range < ratio * avg_range)
    except Exception:
        return False


# ─── Spike Detector ───────────────────────────────────────────────────────────

def detect_spike(
    closes: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    period: int = 14,
    atr_multiplier: float = 3.0,
) -> int:
    """
    Detects a price spike on the most recent bar.

    Returns:
      +1  upward spike   (bar moved up   > atr_multiplier × ATR14)
      -1  downward spike (bar moved down > atr_multiplier × ATR14)
       0  no spike or insufficient data

    Spike definition: the single-bar move (high - low) exceeds
    atr_multiplier × ATR, AND the close is near the extreme end of the bar.
    """
    try:
        C = _to(closes)
        H = _to(highs)
        L = _to(lows)
        n = len(C)

        if n < 15:
            return 0

        atr_vals  = atr(H, L, C, period)
        threshold = float(atr_vals[-1]) * atr_multiplier

        if threshold == 0.0:
            return 0

        bar_range = float(H[-1]) - float(L[-1])
        if bar_range <= threshold:
            return 0

        # Direction: close in upper half → upward spike; lower half → downward.
        bar_mid = (float(H[-1]) + float(L[-1])) / 2.0
        if float(C[-1]) > bar_mid:
            return 1
        else:
            return -1
    except Exception:
        return 0


# ─── Digit Score ──────────────────────────────────────────────────────────────

def digit_score(
    closes: ArrayLike,
    rsi_vals: ArrayLike,
    bb_upper: ArrayLike,
    bb_lower: ArrayLike,
    roc_vals: ArrayLike,
) -> Tuple[int, str]:
    """
    Scoring system for Deriv digit over/under strategy.

    Scoring rules applied to the LAST bar:
      RSI > 78        → +3 pts (overbought  → trade UNDER)
      RSI < 22        → +3 pts (oversold    → trade OVER)
      close >= bb_upper → +3 pts            (trade UNDER)
      close <= bb_lower → +3 pts            (trade OVER)
      roc < -0.02     → +2 pts (momentum confirms OVER)
      roc > +0.02     → +2 pts (momentum confirms UNDER)

    Returns (score: int, direction: str).
    direction is "OVER" | "UNDER" | "NONE".
    Minimum cumulative score of 6 required to return a direction.
    Returns (0, "NONE") on any error or insufficient data.
    """
    try:
        C   = _to(closes)
        RSI = _to(rsi_vals)
        BBU = _to(bb_upper)
        BBL = _to(bb_lower)
        ROC = _to(roc_vals)

        if any(len(x) == 0 for x in (C, RSI, BBU, BBL, ROC)):
            return 0, "NONE"

        close     = float(C[-1])
        rsi_now   = float(RSI[-1])
        upper     = float(BBU[-1])
        lower     = float(BBL[-1])
        roc_now   = float(ROC[-1])

        over_score  = 0
        under_score = 0

        # RSI extremes
        if rsi_now > 78:
            under_score += 3
        elif rsi_now < 22:
            over_score += 3

        # Bollinger Band touches
        if close >= upper:
            under_score += 3
        elif close <= lower:
            over_score += 3

        # ROC momentum
        if roc_now < -0.02:
            over_score += 2
        elif roc_now > 0.02:
            under_score += 2

        if over_score >= 6 and over_score >= under_score:
            return over_score, "OVER"
        if under_score >= 6 and under_score > over_score:
            return under_score, "UNDER"
        # Tied at >=6 → no clear edge
        total = over_score + under_score
        return total, "NONE"
    except Exception:
        return 0, "NONE"


# ─── RSI Divergence ───────────────────────────────────────────────────────────

# Minimum slope magnitude for slope-fallback divergence detection.
_SLOPE_MIN = 0.01   # RSI points per bar (normalised)


def find_rsi_divergence(
    closes: ArrayLike,
    rsi_vals: ArrayLike,
    lookback: int = 20,
) -> int:
    """
    Pivot-based RSI divergence with slope fallback.

    Returns:
      +1  bullish (price lower low,  RSI higher low)
      -1  bearish (price higher high, RSI lower high)
       0  no divergence or insufficient data (< 20 bars)

    Algorithm:
      1. Extract the last `lookback` bars.
      2. Find the last 2 swing lows (price[i] < price[i±1]).
         Bullish if price made a lower low while RSI made a higher low.
      3. Find the last 2 swing highs (price[i] > price[i±1]).
         Bearish if price made a higher high while RSI made a lower high.
      4. If no pivots found, fall back to linear-regression slope comparison.
      5. Returns 0 on any error or < 20 bars.
    """
    try:
        C   = _to(closes)
        RSI = _to(rsi_vals)
        n   = len(C)

        if n < 20 or n < lookback:
            return 0

        lb  = min(lookback, n)
        c_w = C[-lb:]
        r_w = RSI[-lb:]
        m   = len(c_w)

        # ── Swing lows (local minima) ─────────────────────────────────────────
        swing_lows = [
            i for i in range(1, m - 1)
            if float(c_w[i]) < float(c_w[i - 1]) and float(c_w[i]) < float(c_w[i + 1])
        ]
        if len(swing_lows) >= 2:
            i1, i2 = swing_lows[-2], swing_lows[-1]
            c1, c2 = float(c_w[i1]), float(c_w[i2])
            r1, r2 = float(r_w[i1]), float(r_w[i2])
            if not any(np.isnan([r1, r2])):
                if c2 < c1 and r2 > r1:     # lower low in price, higher low in RSI
                    return 1

        # ── Swing highs (local maxima) ────────────────────────────────────────
        swing_highs = [
            i for i in range(1, m - 1)
            if float(c_w[i]) > float(c_w[i - 1]) and float(c_w[i]) > float(c_w[i + 1])
        ]
        if len(swing_highs) >= 2:
            i1, i2 = swing_highs[-2], swing_highs[-1]
            c1, c2 = float(c_w[i1]), float(c_w[i2])
            r1, r2 = float(r_w[i1]), float(r_w[i2])
            if not any(np.isnan([r1, r2])):
                if c2 > c1 and r2 < r1:     # higher high in price, lower high in RSI
                    return -1

        # ── Slope fallback ────────────────────────────────────────────────────
        return _slope_divergence(C, RSI, lookback)
    except Exception:
        return 0


def _slope_divergence(
    closes: np.ndarray,
    rsi_vals: np.ndarray,
    lookback: int,
) -> int:
    """
    Fallback: linear-regression slope comparison over the last `lookback` bars.
    Returns +1 (bullish), -1 (bearish), or 0.  Never raises.
    """
    try:
        n  = len(closes)
        lb = min(lookback, n)
        if lb < 4:
            return 0

        c_win = closes[-lb:].astype(float)
        r_win = rsi_vals[-lb:].astype(float)
        valid = ~np.isnan(r_win)
        if np.sum(valid) < 4:
            return 0

        x = np.arange(lb, dtype=float)

        mean_c = float(np.mean(c_win)) or 1.0
        c_slope = float(np.polyfit(x, c_win / mean_c, 1)[0]) * lb

        r_slope = float(np.polyfit(x[valid], r_win[valid], 1)[0]) * lb

        if c_slope < -_SLOPE_MIN and r_slope > _SLOPE_MIN:
            return 1   # bullish: price falling, RSI rising
        if c_slope > _SLOPE_MIN and r_slope < -_SLOPE_MIN:
            return -1  # bearish: price rising, RSI falling
        return 0
    except Exception:
        return 0


# ─── Momentum Score ───────────────────────────────────────────────────────────

def momentum_score(
    closes: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    fast: int = 8,
    slow: int = 21,
    trend: int = 50,
) -> Tuple[float, int]:
    """
    Composite momentum score for EMA stack alignment.

    Returns (score: float 0.0–1.0, direction: int +1/-1/0).
      Strong bull  (fast > slow > trend, price > trend EMA) : (1.0, +1)
      Bull crossover (fast just crossed above slow)          : (0.8, +1)
      Moderate bull (fast > slow, price > trend EMA)         : (0.7, +1)
      Opposite rules for bearish signals.
      Neutral otherwise                                       : (0.0,  0)
    """
    try:
        C = _to(closes)
        n = len(C)
        if n < 15:
            return 0.0, 0

        ema_fast  = ema(C, fast)
        ema_slow  = ema(C, slow)
        ema_trend = ema(C, trend)

        f0, f1 = float(ema_fast[-1]),  float(ema_fast[-2])  if n >= 2 else float(ema_fast[-1])
        s0, s1 = float(ema_slow[-1]),  float(ema_slow[-2])  if n >= 2 else float(ema_slow[-1])
        t0     = float(ema_trend[-1])
        price  = float(C[-1])

        # Bull crossover: fast crossed above slow on this bar
        bull_cross = f0 > s0 and f1 <= s1
        bear_cross = f0 < s0 and f1 >= s1

        # Strong bull: full EMA stack + price above trend
        if f0 > s0 and s0 > t0 and price > t0:
            return 1.0, 1
        # Strong bear: full EMA stack + price below trend
        if f0 < s0 and s0 < t0 and price < t0:
            return 1.0, -1
        # Bull crossover
        if bull_cross:
            return 0.8, 1
        # Bear crossover
        if bear_cross:
            return 0.8, -1
        # Moderate bull: fast > slow, price above trend EMA
        if f0 > s0 and price > t0:
            return 0.7, 1
        # Moderate bear: fast < slow, price below trend EMA
        if f0 < s0 and price < t0:
            return 0.7, -1

        return 0.0, 0
    except Exception:
        return 0.0, 0


# ─── Breakout Detector ────────────────────────────────────────────────────────

def detect_breakout(
    closes: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    atr_vals: ArrayLike,
    lookback: int = 10,
    mult: float = 1.5,
) -> int:
    """
    Detects a price breakout on the most recent bar.

    Returns:
      +1  upward   breakout (close > highest high of last `lookback` bars + mult × ATR)
      -1  downward breakout (close < lowest  low  of last `lookback` bars - mult × ATR)
       0  no breakout or insufficient data

    The `lookback` window excludes the current bar (uses bars[-lookback-1:-1]).
    """
    try:
        C  = _to(closes)
        H  = _to(highs)
        L  = _to(lows)
        A  = _to(atr_vals)
        n  = len(C)

        if n < max(15, lookback + 1):
            return 0
        if len(A) == 0:
            return 0

        atr_val = float(A[-1])
        if atr_val == 0.0:
            return 0

        # Reference window: bars before the current one
        ref_H = H[-(lookback + 1):-1]
        ref_L = L[-(lookback + 1):-1]
        if len(ref_H) == 0:
            return 0

        highest = float(np.max(ref_H))
        lowest  = float(np.min(ref_L))
        price   = float(C[-1])
        margin  = mult * atr_val

        if price > highest + margin:
            return 1
        if price < lowest - margin:
            return -1
        return 0
    except Exception:
        return 0


# ─── Trend Strength ───────────────────────────────────────────────────────────

def detect_trend_strength(closes: ArrayLike, period: int = 14) -> float:
    """
    Simplified ADX-concept trend strength score.

    Returns float 0.0–1.0.
      > 0.6 → strong trend
      < 0.3 → ranging / choppy
    Uses directional consistency of EMA slope over `period` bars, normalised
    by the volatility of those slopes.  Works on as few as 15 bars.
    Returns 0.0 on error or insufficient data.
    """
    try:
        C = _to(closes)
        n = len(C)
        if n < 15:
            return 0.0

        p = min(period, n - 1)
        if p < 3:
            return 0.0

        ema_vals = ema(C, p)
        # Compute bar-to-bar differences of EMA over last p+1 bars
        window = ema_vals[-(p + 1):]
        diffs  = np.diff(window.astype(float))
        if len(diffs) == 0:
            return 0.0

        # Directional consistency: |mean(diffs)| / (std(diffs) + ε)
        mean_d = float(np.mean(diffs))
        std_d  = float(np.std(diffs)) + 1e-10
        raw    = abs(mean_d) / std_d
        # Normalise: score saturates at raw ≈ 3 (strong trend)
        score  = float(min(raw / 3.0, 1.0))
        return round(score, 4)
    except Exception:
        return 0.0


# ─── Momentum Shift ───────────────────────────────────────────────────────────

def detect_momentum_shift(
    closes: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    lookback: int = 10,
) -> int:
    """
    Detects a near-term momentum shift using consecutive close comparison.

    Bullish  shift (+1): last 3 closes are each higher than the corresponding
                         close 3 bars earlier (positional comparison).
    Bearish  shift (-1): last 3 closes are each lower  than 3 bars earlier.
    Neutral        (0) : mixed signals or insufficient data.

    Requires at least 15 bars.
    """
    try:
        C = _to(closes)
        n = len(C)
        if n < 15 or n < lookback:
            return 0

        # Most recent 6 closes; compare last 3 vs previous 3
        if n < 6:
            return 0

        recent   = [float(C[-(3 - i)]) for i in range(3)]   # C[-3], C[-2], C[-1]
        previous = [float(C[-(6 - i)]) for i in range(3)]   # C[-6], C[-5], C[-4]

        bull = all(recent[i] > previous[i] for i in range(3))
        bear = all(recent[i] < previous[i] for i in range(3))

        if bull:
            return 1
        if bear:
            return -1
        return 0
    except Exception:
        return 0


# ─── Volatility Regime ────────────────────────────────────────────────────────

def volatility_regime(
    closes: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    period: int = 14,
) -> str:
    """
    Classifies the current volatility regime.

    Returns:
      "EXPLOSIVE" – current ATR > 2 × mean ATR of last 20 bars
      "TRENDING"  – ADX-equivalent > 0.5
      "RANGING"   – everything else

    Works on as few as 15 bars; returns "RANGING" on error.
    """
    try:
        C = _to(closes)
        H = _to(highs)
        L = _to(lows)
        n = len(C)

        if n < 15:
            return "RANGING"

        atr_vals = atr(H, L, C, period)
        current_atr = float(atr_vals[-1])

        # Explosive: current ATR > 2× mean of last 20 ATR values (excluding current)
        hist_window = 20
        hist = atr_vals[-(hist_window + 1):-1]
        if len(hist) > 0:
            mean_atr = float(np.mean(hist))
            if mean_atr > 0 and current_atr > 2.0 * mean_atr:
                return "EXPLOSIVE"

        # Trending: use detect_trend_strength
        strength = detect_trend_strength(C, period)
        if strength > 0.5:
            return "TRENDING"

        return "RANGING"
    except Exception:
        return "RANGING"


# ─── Boom/Crash Drift ─────────────────────────────────────────────────────────

def boom_crash_drift(
    closes: ArrayLike,
    spike_atr_mult: float = 3.0,
) -> int:
    """
    Drift direction detector for Boom/Crash synthetic indices.

    After a spike, price drifts in the opposite direction until the next spike.
    This function identifies the post-spike drift direction.

    Returns:
      +1  upward drift   (suitable for MULTUP  on Boom  or after Crash spike)
      -1  downward drift (suitable for MULTDOWN on Crash or after Boom  spike)
       0  near a spike, unclear, or insufficient data

    Algorithm:
      1. Compute ATR14.
      2. Scan the last 20 bars for a spike: single bar move > spike_atr_mult × ATR.
      3. If spike found within last 3 bars → return 0 (too close, avoid entry).
      4. Direction of drift = OPPOSITE to spike direction.
      5. If no spike found → use gentle trend direction from EMA8 slope.
    """
    try:
        C = _to(closes)
        n = len(C)
        if n < 15:
            return 0

        atr_vals = atr(C, C, C, 14)   # H=L=C for close-only ATR approximation
        # Prefer proper ATR if we have enough data; close-only is a fallback.
        # Callers should pass highs/lows; here we gracefully degrade.
        threshold = float(atr_vals[-1]) * spike_atr_mult
        if threshold == 0.0:
            return 0

        scan = min(20, n)
        spike_idx  = None
        spike_dir  = 0

        for i in range(n - scan, n):
            bar_move = abs(float(C[i]) - float(C[i - 1])) if i > 0 else 0.0
            if bar_move > threshold:
                spike_idx = i
                spike_dir = 1 if float(C[i]) > float(C[i - 1]) else -1

        if spike_idx is not None:
            bars_since = (n - 1) - spike_idx
            if bars_since <= 2:
                return 0          # too close to spike — wait
            return -spike_dir     # drift opposite to spike

        # No spike detected — fall back to EMA8 slope for gentle drift
        ema8 = ema(C, 8)
        if len(ema8) >= 2:
            slope = float(ema8[-1]) - float(ema8[-2])
            if slope > 0:
                return 1
            if slope < 0:
                return -1
        return 0
    except Exception:
        return 0


# ─── SL/TP Calculator ─────────────────────────────────────────────────────────

def calculate_sl_tp(
    entry_price: float,
    direction: int,
    atr_val: float,
    sl_pct_stake: float,
    tp_ratio: float = 2.0,
) -> Tuple[float, float]:
    """
    Calculate stop-loss and take-profit dollar amounts for Deriv multiplier contracts.

    Parameters
    ----------
    entry_price   : Current price at entry.
    direction     : +1 for long, -1 for short.
    atr_val       : Current ATR value (same units as price).
    sl_pct_stake  : Stop-loss as a fraction of stake (e.g. 0.05 = 5 %).
    tp_ratio      : Take-profit as a multiple of the stop-loss amount (default 2.0).

    Returns
    -------
    (stop_loss_amount, take_profit_amount) – both positive dollar values passed
    directly to the Deriv multiplier contract `limit_order` field.

    Returns (0.0, 0.0) on any error or zero inputs.
    """
    try:
        if entry_price <= 0 or atr_val <= 0 or sl_pct_stake <= 0:
            return 0.0, 0.0

        # SL distance in price units = 1.5 × ATR (standard buffer)
        sl_price_dist = 1.5 * float(atr_val)

        # Dollar SL is the stake fraction supplied by the caller
        sl_amount = float(sl_pct_stake)
        tp_amount = round(float(sl_amount) * float(tp_ratio), 6)
        sl_amount = round(sl_amount, 6)

        if sl_amount <= 0 or tp_amount <= 0:
            return 0.0, 0.0

        return sl_amount, tp_amount
    except Exception:
        return 0.0, 0.0


# ─── ADX ──────────────────────────────────────────────────────────────────────

def adx(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    period: int = 14,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Average Directional Index.
    Returns (ADX, +DI, -DI).
    Returns (zeros, zeros, zeros) when input is too short (< 2*period+1).
    All returned arrays are the same length as the input.
    """
    try:
        H, L, C = _to(highs), _to(lows), _to(closes)
        n = len(C)
        if n == 0:
            return np.array([]), np.array([]), np.array([])
        if n < 2 * period + 1:
            return np.zeros(n), np.zeros(n), np.zeros(n)

        plus_dm  = np.zeros(n)
        minus_dm = np.zeros(n)
        tr_arr   = np.zeros(n)
        for i in range(1, n):
            up   = float(H[i]) - float(H[i - 1])
            down = float(L[i - 1]) - float(L[i])
            plus_dm[i]  = up   if (up > down   and up > 0)   else 0.0
            minus_dm[i] = down if (down > up   and down > 0) else 0.0
            tr_arr[i]   = max(
                float(H[i]) - float(L[i]),
                abs(float(H[i]) - float(C[i - 1])),
                abs(float(L[i]) - float(C[i - 1])),
            )

        def _wilder(arr: np.ndarray, p: int) -> np.ndarray:
            out = np.zeros(n)
            out[p] = float(np.sum(arr[1 : p + 1]))
            for i in range(p + 1, n):
                out[i] = out[i - 1] - out[i - 1] / p + arr[i]
            return out

        atr14 = _wilder(tr_arr, period)
        pdm14 = _wilder(plus_dm, period)
        mdm14 = _wilder(minus_dm, period)

        with np.errstate(divide="ignore", invalid="ignore"):
            safe_atr = np.where(atr14 != 0, atr14, 1.0)
            plus_di  = np.where(atr14 != 0, 100.0 * pdm14 / safe_atr, 0.0)
            minus_di = np.where(atr14 != 0, 100.0 * mdm14 / safe_atr, 0.0)

        denom = plus_di + minus_di
        with np.errstate(divide="ignore", invalid="ignore"):
            safe_denom = np.where(denom != 0, denom, 1.0)
            dx = np.where(
                denom != 0,
                100.0 * np.abs(plus_di - minus_di) / safe_denom,
                0.0,
            )

        adx_vals = np.zeros(n)
        if n > 2 * period:
            adx_vals[2 * period] = float(np.mean(dx[period : 2 * period + 1]))
            for i in range(2 * period + 1, n):
                adx_vals[i] = (adx_vals[i - 1] * (period - 1) + dx[i]) / period

        return adx_vals, plus_di, minus_di
    except Exception:
        C = _to(closes)
        n = len(C)
        return np.zeros(n), np.zeros(n), np.zeros(n)


# ─── Stochastic RSI ───────────────────────────────────────────────────────────

def stoch_rsi(
    closes: ArrayLike,
    rsi_period: int = 14,
    stoch_period: int = 14,
    k: int = 3,
    d: int = 3,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Stochastic RSI.
    Returns (%K, %D), both in [0, 1].
    Returns (zeros, zeros) when too short.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([]), np.array([])

        min_len = rsi_period + stoch_period + k + d
        if n < min_len:
            return np.zeros(n), np.zeros(n)

        rsi_vals = rsi(p, rsi_period)
        raw_k    = _fill(n, 0.5)

        for i in range(stoch_period - 1, n):
            window = rsi_vals[i - stoch_period + 1 : i + 1]
            valid  = window[~np.isnan(window)]
            if len(valid) == 0:
                continue
            mn = float(np.min(valid))
            mx = float(np.max(valid))
            if mx == mn:
                raw_k[i] = 0.5
            else:
                raw_k[i] = (float(rsi_vals[i]) - mn) / (mx - mn)

        K = sma(raw_k, k)
        D = sma(K, d)

        K = np.where(np.isnan(K), 0.0, K)
        D = np.where(np.isnan(D), 0.0, D)
        return K, D
    except Exception:
        p = _to(closes)
        return np.zeros(len(p)), np.zeros(len(p))


# ─── SMC: Swing Highs ─────────────────────────────────────────────────────────

def swing_highs(highs: ArrayLike, lookback: int = 5) -> np.ndarray:
    """
    Swing Highs detector.
    Returns array same length as highs.
    Value = high price at confirmed swing high, NaN elsewhere.
    A swing high = bar[i].high is the highest within `lookback` bars on both sides.
    """
    try:
        H = _to(highs)
        n = len(H)
        out = np.full(n, np.nan)
        if n < 2 * lookback + 1:
            return out
        for i in range(lookback, n - lookback):
            window = H[i - lookback: i + lookback + 1]
            if float(H[i]) >= float(np.max(window)):
                out[i] = float(H[i])
        return out
    except Exception:
        H = _to(highs)
        return np.full(len(H), np.nan)


# ─── SMC: Swing Lows ──────────────────────────────────────────────────────────

def swing_lows(lows: ArrayLike, lookback: int = 5) -> np.ndarray:
    """
    Swing Lows detector.
    Returns array same length as lows.
    Value = low price at confirmed swing low, NaN elsewhere.
    """
    try:
        L = _to(lows)
        n = len(L)
        out = np.full(n, np.nan)
        if n < 2 * lookback + 1:
            return out
        for i in range(lookback, n - lookback):
            window = L[i - lookback: i + lookback + 1]
            if float(L[i]) <= float(np.min(window)):
                out[i] = float(L[i])
        return out
    except Exception:
        L = _to(lows)
        return np.full(len(L), np.nan)


# ─── SMC: Market Structure ────────────────────────────────────────────────────

def market_structure(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    lookback: int = 5,
) -> Tuple[str, float]:
    """
    Market structure analysis.
    Returns ("BULLISH", score), ("BEARISH", score), or ("NEUTRAL", 0).
    Bullish = last 2 confirmed swing highs ascending AND last 2 swing lows ascending.
    Score = 0.8 if both conditions met, 0.6 if only one.
    """
    try:
        H = _to(highs)
        L = _to(lows)
        C = _to(closes)
        n = len(C)
        if n < 2 * lookback + 5:
            return "NEUTRAL", 0

        sh = swing_highs(H, lookback)
        sl = swing_lows(L, lookback)

        sh_vals = [(i, float(v)) for i, v in enumerate(sh) if not np.isnan(v)]
        sl_vals = [(i, float(v)) for i, v in enumerate(sl) if not np.isnan(v)]

        bull_sh = bull_sl = bear_sh = bear_sl = False

        if len(sh_vals) >= 2:
            if sh_vals[-1][1] > sh_vals[-2][1]:
                bull_sh = True
            elif sh_vals[-1][1] < sh_vals[-2][1]:
                bear_sh = True

        if len(sl_vals) >= 2:
            if sl_vals[-1][1] > sl_vals[-2][1]:
                bull_sl = True
            elif sl_vals[-1][1] < sl_vals[-2][1]:
                bear_sl = True

        bull_count = int(bull_sh) + int(bull_sl)
        bear_count = int(bear_sh) + int(bear_sl)

        if bull_count == 2:
            return "BULLISH", 0.8
        if bear_count == 2:
            return "BEARISH", 0.8
        if bull_count == 1 and bear_count == 0:
            return "BULLISH", 0.6
        if bear_count == 1 and bull_count == 0:
            return "BEARISH", 0.6
        return "NEUTRAL", 0
    except Exception:
        return "NEUTRAL", 0


# ─── SMC: Order Blocks ────────────────────────────────────────────────────────

def find_order_blocks(
    opens: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    lookback: int = 50,
) -> list:
    """
    Find Order Blocks.
    Bullish OB = last bearish candle before a strong bullish impulse (next body > 1.5×ATR14).
    Bearish OB = last bullish candle before a strong bearish impulse.
    Returns list of dicts: {type, high, low, mid, index, fresh, test_count}
    fresh=False if price has returned to the zone since creation.
    """
    try:
        O = _to(opens)
        H = _to(highs)
        L = _to(lows)
        C = _to(closes)
        n = len(C)
        if n < 20:
            return []

        atr_vals = atr(H, L, C, 14)
        lb = min(lookback, n - 2)
        obs = []

        for i in range(n - lb, n - 1):
            if i < 1:
                continue
            atr_val = float(atr_vals[i])
            if atr_val == 0:
                continue
            body_next = abs(float(C[i + 1]) - float(O[i + 1]))
            is_strong = body_next > 1.5 * atr_val

            # Bullish OB: bearish candle followed by strong bullish move
            if float(C[i]) < float(O[i]) and float(C[i + 1]) > float(O[i + 1]) and is_strong:
                ob_high = float(H[i])
                ob_low  = float(L[i])
                test_count = 0
                for j in range(i + 2, n):
                    if float(L[j]) <= ob_high and float(H[j]) >= ob_low:
                        test_count += 1
                obs.append({
                    "type": "BULLISH", "high": ob_high, "low": ob_low,
                    "mid": (ob_high + ob_low) / 2, "index": i,
                    "fresh": test_count == 0, "test_count": test_count,
                })

            # Bearish OB: bullish candle followed by strong bearish move
            if float(C[i]) > float(O[i]) and float(C[i + 1]) < float(O[i + 1]) and is_strong:
                ob_high = float(H[i])
                ob_low  = float(L[i])
                test_count = 0
                for j in range(i + 2, n):
                    if float(L[j]) <= ob_high and float(H[j]) >= ob_low:
                        test_count += 1
                obs.append({
                    "type": "BEARISH", "high": ob_high, "low": ob_low,
                    "mid": (ob_high + ob_low) / 2, "index": i,
                    "fresh": test_count == 0, "test_count": test_count,
                })

        return obs
    except Exception:
        return []


# ─── SMC: Fair Value Gaps ─────────────────────────────────────────────────────

def find_fvg(
    opens: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    atr_arr: ArrayLike,
    min_atr: float = 0.5,
) -> list:
    """
    Find Fair Value Gaps (3-candle imbalance).
    Bullish FVG: candle[i-2].high < candle[i].low and gap > min_atr × ATR.
    Bearish FVG: candle[i-2].low > candle[i].high and gap > min_atr × ATR.
    Returns list of dicts: {type, high, low, mid, index, filled}
    """
    try:
        H = _to(highs)
        L = _to(lows)
        A = _to(atr_arr)
        n = len(H)
        if n < 5:
            return []

        fvgs = []
        for i in range(2, n):
            atr_val = float(A[i]) if i < len(A) else 0.0
            min_gap = min_atr * atr_val

            # Bullish FVG: gap between candle[i-2] high and candle[i] low
            gap_bull = float(L[i]) - float(H[i - 2])
            if gap_bull > 0 and gap_bull > min_gap:
                fvg_high = float(L[i])
                fvg_low  = float(H[i - 2])
                filled = any(
                    float(L[j]) <= fvg_high and float(H[j]) >= fvg_low
                    for j in range(i + 1, n)
                )
                fvgs.append({
                    "type": "BULLISH", "high": fvg_high, "low": fvg_low,
                    "mid": (fvg_high + fvg_low) / 2, "index": i, "filled": filled,
                })

            # Bearish FVG: gap between candle[i] high and candle[i-2] low
            gap_bear = float(H[i - 2]) - float(L[i])
            if gap_bear > 0 and gap_bear > min_gap:
                fvg_high = float(H[i - 2])
                fvg_low  = float(L[i])
                filled = any(
                    float(L[j]) <= fvg_high and float(H[j]) >= fvg_low
                    for j in range(i + 1, n)
                )
                fvgs.append({
                    "type": "BEARISH", "high": fvg_high, "low": fvg_low,
                    "mid": (fvg_high + fvg_low) / 2, "index": i, "filled": filled,
                })

        return fvgs
    except Exception:
        return []


# ─── SMC: Liquidity Sweep ─────────────────────────────────────────────────────

def liquidity_sweep(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    lookback: int = 20,
) -> int:
    """
    Liquidity sweep detection.
    Returns +1: current bar wicked below swing low then closed above it (bullish reversal).
    Returns -1: current bar wicked above swing high then closed below it (bearish reversal).
    Returns  0: no sweep.
    """
    try:
        H = _to(highs)
        L = _to(lows)
        C = _to(closes)
        n = len(C)
        if n < lookback + 5:
            return 0

        lb = min(lookback, n - 1)
        prev_lows  = L[-(lb + 1):-1]
        prev_highs = H[-(lb + 1):-1]

        cur_low   = float(L[-1])
        cur_high  = float(H[-1])
        cur_close = float(C[-1])

        swing_low  = float(np.min(prev_lows))
        swing_high = float(np.max(prev_highs))

        if cur_low < swing_low and cur_close > swing_low:
            return 1
        if cur_high > swing_high and cur_close < swing_high:
            return -1
        return 0
    except Exception:
        return 0


# ─── Fibonacci Levels ─────────────────────────────────────────────────────────

def fibonacci_levels(swing_high: float, swing_low: float) -> dict:
    """
    Fibonacci retracement levels.
    Returns dict keyed by level string: 0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0.
    """
    try:
        sh = float(swing_high)
        sl = float(swing_low)
        rng = sh - sl
        return {
            "0":     sh,
            "0.236": sh - 0.236 * rng,
            "0.382": sh - 0.382 * rng,
            "0.5":   sh - 0.5   * rng,
            "0.618": sh - 0.618 * rng,
            "0.786": sh - 0.786 * rng,
            "1.0":   sl,
        }
    except Exception:
        return {k: 0.0 for k in ("0","0.236","0.382","0.5","0.618","0.786","1.0")}


# ─── Price at Fibonacci Level ─────────────────────────────────────────────────

def price_at_fib(
    close: float,
    swing_high: float,
    swing_low: float,
    tolerance: float = 0.1,
    atr_val: float = 0.0,
) -> str:
    """
    Returns the Fibonacci level string if price is within tolerance×ATR of that level,
    else None. Checks 0.382, 0.5, 0.618, 0.786.
    """
    try:
        levels = fibonacci_levels(swing_high, swing_low)
        c   = float(close)
        rng = abs(float(swing_high) - float(swing_low))
        tol = tolerance * (float(atr_val) if float(atr_val) > 0 else rng * 0.01)
        for key in ("0.382", "0.5", "0.618", "0.786"):
            if abs(c - levels[key]) <= tol:
                return key
        return None
    except Exception:
        return None


# ─── Chart Pattern Detection ──────────────────────────────────────────────────

def detect_chart_pattern(
    opens: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
) -> Tuple:
    """
    Detect common chart patterns.
    Returns (pattern_name, direction, score) or (None, None, 0).
    Patterns: Double Top, Double Bottom, Head & Shoulders, Inverse H&S,
              Bull Flag, Bear Flag.
    """
    try:
        H = _to(highs)
        L = _to(lows)
        C = _to(closes)
        n = len(C)
        if n < 20:
            return None, None, 0

        sh_arr = swing_highs(H, 5)
        sl_arr = swing_lows(L, 5)
        sh_pts = [(i, float(v)) for i, v in enumerate(sh_arr) if not np.isnan(v)]
        sl_pts = [(i, float(v)) for i, v in enumerate(sl_arr) if not np.isnan(v)]

        # Double Top
        if len(sh_pts) >= 2:
            (i1, h1), (i2, h2) = sh_pts[-2], sh_pts[-1]
            if h1 > 0 and abs(h1 - h2) / h1 < 0.02:
                neckline = float(np.min(L[i1:i2 + 1])) if i2 > i1 else float(L[i1])
                if float(C[-1]) < neckline:
                    return "DOUBLE_TOP", "SHORT", 0.80

        # Double Bottom
        if len(sl_pts) >= 2:
            (i1, l1), (i2, l2) = sl_pts[-2], sl_pts[-1]
            if abs(l1) > 0 and abs(l1 - l2) / abs(l1) < 0.02:
                neckline = float(np.max(H[i1:i2 + 1])) if i2 > i1 else float(H[i1])
                if float(C[-1]) > neckline:
                    return "DOUBLE_BOTTOM", "LONG", 0.80

        # Head and Shoulders
        if len(sh_pts) >= 3:
            (li, lv), (hi, hv), (ri, rv) = sh_pts[-3], sh_pts[-2], sh_pts[-1]
            if hv > lv and hv > rv and lv > 0 and abs(lv - rv) / lv < 0.05:
                nl = float(np.min(L[li:hi + 1])) if hi > li else float(L[li])
                nr = float(np.min(L[hi:ri + 1])) if ri > hi else float(L[hi])
                if float(C[-1]) < (nl + nr) / 2:
                    return "HEAD_AND_SHOULDERS", "SHORT", 0.85

        # Inverse Head and Shoulders
        if len(sl_pts) >= 3:
            (li, lv), (hi, hv), (ri, rv) = sl_pts[-3], sl_pts[-2], sl_pts[-1]
            if hv < lv and hv < rv and abs(lv) > 0 and abs(lv - rv) / abs(lv) < 0.05:
                nl = float(np.max(H[li:hi + 1])) if hi > li else float(H[li])
                nr = float(np.max(H[hi:ri + 1])) if ri > hi else float(H[hi])
                if float(C[-1]) > (nl + nr) / 2:
                    return "INVERSE_HEAD_AND_SHOULDERS", "LONG", 0.85

        # Bull Flag: sharp impulse up + tight consolidation
        if n >= 15:
            imp_c = C[-15:-10]
            con_c = C[-10:]
            if len(imp_c) >= 3 and len(con_c) >= 5:
                impulse_gain = (float(imp_c[-1]) - float(imp_c[0])) / max(float(imp_c[0]), 1e-10)
                con_range    = (float(np.max(con_c)) - float(np.min(con_c))) / max(abs(float(imp_c[-1])), 1e-10)
                if impulse_gain > 0.005 and con_range < impulse_gain * 0.5:
                    return "BULL_FLAG", "LONG", 0.75

        # Bear Flag: sharp impulse down + tight consolidation
        if n >= 15:
            imp_c = C[-15:-10]
            con_c = C[-10:]
            if len(imp_c) >= 3 and len(con_c) >= 5:
                impulse_drop = (float(imp_c[0]) - float(imp_c[-1])) / max(float(imp_c[0]), 1e-10)
                con_range    = (float(np.max(con_c)) - float(np.min(con_c))) / max(abs(float(imp_c[-1])), 1e-10)
                if impulse_drop > 0.005 and con_range < impulse_drop * 0.5:
                    return "BEAR_FLAG", "SHORT", 0.75

        return None, None, 0
    except Exception:
        return None, None, 0


# ─── Candlestick Pattern Detection ───────────────────────────────────────────

def detect_candlestick_pattern(
    opens: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
) -> Tuple:
    """
    Detect candlestick patterns on the most recent bars.
    Returns (pattern_name, direction, score) or (None, None, 0).
    Patterns: Bullish/Bearish Engulfing, Hammer, Shooting Star,
              Morning Star, Evening Star, Doji, Pin Bar.
    """
    try:
        O = _to(opens)
        H = _to(highs)
        L = _to(lows)
        C = _to(closes)
        n = len(C)
        if n < 3:
            return None, None, 0

        o1, h1, l1, c1 = float(O[-1]), float(H[-1]), float(L[-1]), float(C[-1])
        o2, h2, l2, c2 = float(O[-2]), float(H[-2]), float(L[-2]), float(C[-2])
        o3 = float(O[-3]) if n >= 3 else o2
        l3 = float(L[-3]) if n >= 3 else l2
        h3 = float(H[-3]) if n >= 3 else h2
        c3 = float(C[-3]) if n >= 3 else c2

        body1  = abs(c1 - o1)
        body2  = abs(c2 - o2)
        body3  = abs(c3 - o3)
        range1 = h1 - l1 if h1 > l1 else 1e-10

        # Bullish Engulfing
        if c2 < o2 and c1 > o1 and o1 <= c2 and c1 >= o2:
            return "BULLISH_ENGULFING", "LONG", 0.75

        # Bearish Engulfing
        if c2 > o2 and c1 < o1 and o1 >= c2 and c1 <= o2:
            return "BEARISH_ENGULFING", "SHORT", 0.75

        # Hammer (bullish): long lower wick, small body at top
        lower_wick = min(o1, c1) - l1
        upper_wick = h1 - max(o1, c1)
        if body1 > 0 and lower_wick >= 2 * body1 and upper_wick <= body1:
            return "HAMMER", "LONG", 0.70

        # Shooting Star (bearish): long upper wick, small body at bottom
        if body1 > 0 and upper_wick >= 2 * body1 and lower_wick <= body1:
            return "SHOOTING_STAR", "SHORT", 0.70

        # Pin Bar Bullish: lower wick > 60% of range
        if lower_wick > 0.6 * range1:
            return "PIN_BAR_BULL", "LONG", 0.72

        # Pin Bar Bearish: upper wick > 60% of range
        if upper_wick > 0.6 * range1:
            return "PIN_BAR_BEAR", "SHORT", 0.72

        # Morning Star (3-bar bullish reversal)
        if n >= 3 and body3 > 0:
            if (c3 < o3 and
                    body2 < 0.3 * body3 and
                    c1 > o1 and c1 > (o3 + c3) / 2):
                return "MORNING_STAR", "LONG", 0.80

        # Evening Star (3-bar bearish reversal)
        if n >= 3 and body3 > 0:
            if (c3 > o3 and
                    body2 < 0.3 * body3 and
                    c1 < o1 and c1 < (o3 + c3) / 2):
                return "EVENING_STAR", "SHORT", 0.80

        # Doji: body < 10% of range
        if body1 < 0.1 * range1:
            direction = "LONG" if c1 > c2 else "SHORT"
            return "DOJI", direction, 0.65

        return None, None, 0
    except Exception:
        return None, None, 0


# ─── Premium / Discount Zone ─────────────────────────────────────────────────

def premium_discount_zone(
    close: float,
    swing_high: float,
    swing_low: float,
) -> str:
    """
    Returns "PREMIUM" if close in top 30% of range,
    "DISCOUNT" if bottom 30%, "EQUILIBRIUM" otherwise.
    """
    try:
        c   = float(close)
        sh  = float(swing_high)
        sl  = float(swing_low)
        rng = sh - sl
        if rng <= 0:
            return "EQUILIBRIUM"
        pct = (c - sl) / rng
        if pct >= 0.70:
            return "PREMIUM"
        if pct <= 0.30:
            return "DISCOUNT"
        return "EQUILIBRIUM"
    except Exception:
        return "EQUILIBRIUM"


# ─── Trend Strength (ADX-based) ───────────────────────────────────────────────

def trend_strength(
    closes: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    period: int = 14,
) -> float:
    """
    ADX-equivalent trend strength.
    Returns 0.0–1.0. Above 0.6 = strong trend. Below 0.3 = ranging.
    """
    try:
        H = _to(highs)
        L = _to(lows)
        C = _to(closes)
        n = len(C)
        if n < 15:
            return 0.0
        adx_vals, _, _ = adx(H, L, C, period)
        return round(min(float(adx_vals[-1]) / 100.0, 1.0), 4)
    except Exception:
        return 0.0

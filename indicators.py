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

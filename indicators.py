"""
indicators.py – Pure-numpy implementations of every indicator used by SIFM.

No TA-Lib, no pandas-ta – works on Render's free tier out of the box.
All functions return numpy arrays of the same length as the input,
with np.nan padding where data is insufficient.

v1 → v2 changes (Change 7):

  find_rsi_divergence:
    • Now handles series with as few as 30 bars (was lookback*2+2 ≈ 22 but
      could silently return 0 when RSI had insufficient valid values).
    • Added slope fallback: when the price-pivot approach finds no divergence,
      a linear-regression slope comparison between closes and RSI over the
      last `lookback` bars is used as a secondary signal.  If price slope and
      RSI slope have opposite signs AND both slopes exceed a minimum threshold,
      divergence is inferred (+1 or -1).
    • Always returns a valid int (+1, -1, or 0) for any input ≥ 30 bars.
    • Returns 0 (not raises) for input shorter than 30 bars.

  stoch_rsi:
    • Returns (zeros_array, zeros_array) instead of (nan_array, nan_array)
      when input is too short to compute.  Callers that filter with
      ~np.isnan() still work; callers that use the raw values get 0 rather
      than NaN, preventing downstream errors.

  adx:
    • Returns (zeros_array, zeros_array, zeros_array) when input is too short
      (< period*2+1 bars).  Same rationale as stoch_rsi.
    • The returned arrays are the same length as the input, filled with 0.0,
      so callers that iterate by index or call last_adx[-1] still work.
"""

import numpy as np
from typing import List, Tuple, Union

ArrayLike = Union[List[float], np.ndarray]


def _to(data: ArrayLike) -> np.ndarray:
    return np.asarray(data, dtype=float)


# ─── Moving Averages ──────────────────────────────────────────────────────────

def sma(prices: ArrayLike, period: int) -> np.ndarray:
    p = _to(prices)
    out = np.full(len(p), np.nan)
    for i in range(period - 1, len(p)):
        out[i] = np.mean(p[i - period + 1 : i + 1])
    return out


def ema(prices: ArrayLike, period: int) -> np.ndarray:
    p = _to(prices)
    out = np.full(len(p), np.nan)
    if len(p) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = np.mean(p[:period])
    for i in range(period, len(p)):
        out[i] = p[i] * k + out[i - 1] * (1 - k)
    return out


# ─── RSI ─────────────────────────────────────────────────────────────────────

def rsi(prices: ArrayLike, period: int = 14) -> np.ndarray:
    p = _to(prices)
    out = np.full(len(p), np.nan)
    if len(p) < period + 1:
        return out
    delta  = np.diff(p)
    gains  = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_g  = float(np.mean(gains[:period]))
    avg_l  = float(np.mean(losses[:period]))
    for i in range(period, len(delta)):
        avg_g = (avg_g * (period - 1) + gains[i])  / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


# ─── MACD ────────────────────────────────────────────────────────────────────

def macd(prices: ArrayLike, fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    p = _to(prices)
    fast_ema  = ema(p, fast)
    slow_ema  = ema(p, slow)
    macd_line = fast_ema - slow_ema
    sig_line  = np.full(len(p), np.nan)
    valid_idx = np.where(~np.isnan(macd_line))[0]
    if len(valid_idx) >= signal:
        start     = valid_idx[0]
        sig_vals  = ema(macd_line[start:], signal)
        sig_line[start:] = sig_vals
    hist = macd_line - sig_line
    return macd_line, sig_line, hist


# ─── Bollinger Bands ─────────────────────────────────────────────────────────

def bollinger_bands(prices: ArrayLike, period: int = 20,
                    num_std: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    p   = _to(prices)
    mid = sma(p, period)
    std = np.full(len(p), np.nan)
    for i in range(period - 1, len(p)):
        std[i] = float(np.std(p[i - period + 1 : i + 1], ddof=0))
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


# ─── Stochastic RSI ──────────────────────────────────────────────────────────

def stoch_rsi(prices: ArrayLike, rsi_period: int = 14, stoch_period: int = 14,
              k_smooth: int = 3, d_smooth: int = 3) -> Tuple[np.ndarray, np.ndarray]:
    """
    Returns (%K, %D) both in [0, 1].

    v2: Returns zero arrays (not NaN arrays) when input is too short.
    This ensures callers get 0 rather than NaN, preventing downstream errors
    while remaining compatible with callers that filter on ~np.isnan().
    """
    p = _to(prices)
    n = len(p)
    min_len = rsi_period + stoch_period + k_smooth + d_smooth
    if n < min_len:
        return np.zeros(n), np.zeros(n)

    rsi_vals = rsi(prices, rsi_period)
    raw_k    = np.full(n, np.nan)
    for i in range(stoch_period - 1, n):
        window = rsi_vals[i - stoch_period + 1 : i + 1]
        valid  = window[~np.isnan(window)]
        if len(valid) == 0:
            continue
        mn, mx = float(np.min(valid)), float(np.max(valid))
        raw_k[i] = 0.5 if mx == mn else (rsi_vals[i] - mn) / (mx - mn)
    k = sma(raw_k, k_smooth)
    d = sma(k,     d_smooth)

    # Replace any remaining NaN with 0 for safe downstream use
    k = np.where(np.isnan(k), 0.0, k)
    d = np.where(np.isnan(d), 0.0, d)
    return k, d


# ─── ATR ─────────────────────────────────────────────────────────────────────

def atr(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
        period: int = 14) -> np.ndarray:
    H, L, C = _to(highs), _to(lows), _to(closes)
    n  = len(C)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(H[i] - L[i],
                    abs(H[i] - C[i - 1]),
                    abs(L[i] - C[i - 1]))
    out = np.full(n, np.nan)
    if n > period:
        out[period] = float(np.mean(tr[1 : period + 1]))
        for i in range(period + 1, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


# ─── ADX ─────────────────────────────────────────────────────────────────────

def adx(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
        period: int = 14) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns (ADX, +DI, -DI).

    v2: Returns (zeros, zeros, zeros) when input is too short (< 2*period+1).
    All returned arrays are the same length as the input.
    """
    H, L, C = _to(highs), _to(lows), _to(closes)
    n = len(C)

    # Minimum viable length: need 2*period bars for ADX smoothing
    if n < 2 * period + 1:
        return np.zeros(n), np.zeros(n), np.zeros(n)

    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    tr_arr   = np.zeros(n)
    for i in range(1, n):
        up   = H[i] - H[i - 1]
        down = L[i - 1] - L[i]
        plus_dm[i]  = up   if (up > down   and up > 0)   else 0.0
        minus_dm[i] = down if (down > up   and down > 0) else 0.0
        tr_arr[i]   = max(H[i] - L[i], abs(H[i] - C[i-1]), abs(L[i] - C[i-1]))

    def wilder(arr, p):
        out = np.zeros(n)
        out[p] = float(np.sum(arr[1:p+1]))
        for i in range(p+1, n):
            out[i] = out[i-1] - out[i-1]/p + arr[i]
        return out

    atr14     = wilder(tr_arr, period)
    pdm14     = wilder(plus_dm, period)
    mdm14     = wilder(minus_dm, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di  = np.where(atr14 != 0, 100 * pdm14 / np.where(atr14 != 0, atr14, 1), 0.0)
        minus_di = np.where(atr14 != 0, 100 * mdm14 / np.where(atr14 != 0, atr14, 1), 0.0)
    denom     = plus_di + minus_di
    with np.errstate(divide="ignore", invalid="ignore"):
        dx = np.where(denom != 0,
                      100 * np.abs(plus_di - minus_di) / np.where(denom != 0, denom, 1),
                      0.0)
    adx_vals  = np.zeros(n)   # v2: zeros instead of NaN for graceful short-input
    if n > 2 * period:
        adx_vals[2*period] = float(np.mean(dx[period : 2*period+1]))
        for i in range(2*period+1, n):
            adx_vals[i] = (adx_vals[i-1] * (period-1) + dx[i]) / period
    return adx_vals, plus_di, minus_di


# ─── Helper: find divergence ──────────────────────────────────────────────────

# Minimum slope magnitude for the slope-fallback divergence detection.
# Below this threshold the slope is considered flat (no divergence).
_SLOPE_MIN_THRESHOLD = 0.01   # RSI points per bar

def find_rsi_divergence(closes: np.ndarray, rsi_vals: np.ndarray,
                        lookback: int = 20) -> int:
    """
    Returns:
      +1 if bullish regular divergence (price lower low, RSI higher low)
      -1 if bearish regular divergence (price higher high, RSI lower high)
       0 if no divergence or insufficient data

    v2 improvements:
      • Works for series ≥ 30 bars (shorter → returns 0, never raises).
      • Primary method: pivot-based (unchanged from v1).
      • Slope fallback: when pivots give no result, compare linear-regression
        slopes of closes and rsi_vals over the last `lookback` bars.
        If price slope is negative and RSI slope is positive → bullish div (+1).
        If price slope is positive and RSI slope is negative → bearish div (-1).
        Both slopes must exceed _SLOPE_MIN_THRESHOLD to fire.
    """
    closes   = np.asarray(closes, dtype=float)
    rsi_vals = np.asarray(rsi_vals, dtype=float)

    n = len(closes)
    # Require at least 30 bars total
    if n < 30:
        return 0

    # Need at least 2×lookback bars for the pivot comparison windows
    if n < lookback * 2 + 2:
        # Fall through directly to slope fallback
        return _slope_divergence(closes, rsi_vals, lookback)

    recent_c   = closes[-lookback:]
    recent_r   = rsi_vals[-lookback:]
    valid_mask = ~np.isnan(recent_r)
    if np.sum(valid_mask) < 4:
        return _slope_divergence(closes, rsi_vals, lookback)

    # Bullish divergence: price lower low, RSI higher low
    price_min_idx = int(np.argmin(recent_c))
    rsi_at_min    = recent_r[price_min_idx] if not np.isnan(recent_r[price_min_idx]) else np.nan

    prev_c = closes[-lookback*2 : -lookback] if n >= lookback*2 else closes[:max(1,n-lookback)]
    prev_r = rsi_vals[-lookback*2 : -lookback] if n >= lookback*2 else rsi_vals[:max(1,n-lookback)]
    if len(prev_c) == 0:
        return _slope_divergence(closes, rsi_vals, lookback)

    prev_min_c   = float(np.min(prev_c))
    prev_min_idx = int(np.argmin(prev_c))
    prev_r_valid = prev_r[~np.isnan(prev_r)]
    if len(prev_r_valid) == 0:
        return _slope_divergence(closes, rsi_vals, lookback)
    prev_min_r   = prev_r[prev_min_idx] if not np.isnan(prev_r[prev_min_idx]) else float(np.nanmin(prev_r))

    if not np.isnan(rsi_at_min) and not np.isnan(prev_min_r):
        current_min_c = float(recent_c[price_min_idx])
        if current_min_c < prev_min_c and rsi_at_min > prev_min_r:
            price_delta = abs(current_min_c - prev_min_c)
            strength = abs(rsi_at_min - prev_min_r) / (price_delta + 1e-10)
            if strength > 0.3:
                return 1

    # Bearish divergence: price higher high, RSI lower high
    price_max_idx = int(np.argmax(recent_c))
    rsi_at_max    = recent_r[price_max_idx] if not np.isnan(recent_r[price_max_idx]) else np.nan
    prev_max_c    = float(np.max(prev_c))
    prev_max_idx  = int(np.argmax(prev_c))
    prev_max_r    = prev_r[prev_max_idx] if not np.isnan(prev_r[prev_max_idx]) else float(np.nanmax(prev_r))

    if not np.isnan(rsi_at_max) and not np.isnan(prev_max_r):
        current_max_c = float(recent_c[price_max_idx])
        if current_max_c > prev_max_c and rsi_at_max < prev_max_r:
            price_delta = abs(current_max_c - prev_max_c)
            strength = abs(rsi_at_max - prev_max_r) / (price_delta + 1e-10)
            if strength > 0.3:
                return -1

    # Pivot method gave no result → try slope fallback
    return _slope_divergence(closes, rsi_vals, lookback)


def _slope_divergence(closes: np.ndarray, rsi_vals: np.ndarray,
                      lookback: int) -> int:
    """
    Slope-based divergence fallback.
    Uses linear regression over the last `lookback` bars.

    Returns +1 (bullish), -1 (bearish), or 0.
    """
    n = len(closes)
    lb = min(lookback, n)
    if lb < 4:
        return 0

    c_window = closes[-lb:].astype(float)
    r_window = rsi_vals[-lb:].astype(float)

    # Drop NaN from RSI window
    valid = ~np.isnan(r_window)
    if np.sum(valid) < 4:
        return 0

    x = np.arange(lb, dtype=float)

    # Price slope (normalised by mean price to make it unit-free)
    mean_c = float(np.mean(c_window)) if float(np.mean(c_window)) != 0 else 1.0
    c_poly = np.polyfit(x, c_window / mean_c, 1)
    price_slope = c_poly[0] * lb   # total normalised change over window

    # RSI slope
    r_poly = np.polyfit(x[valid], r_window[valid], 1)
    rsi_slope = r_poly[0] * lb   # total RSI change over window (RSI points)

    thresh = _SLOPE_MIN_THRESHOLD

    # Bullish: price falling, RSI rising
    if price_slope < -thresh and rsi_slope > thresh:
        return 1

    # Bearish: price rising, RSI falling
    if price_slope > thresh and rsi_slope < -thresh:
        return -1

    return 0

"""
indicators.py – Pure-numpy implementations of every indicator used by SIFM.

No TA-Lib, no pandas-ta – works on Render's free tier out of the box.
All functions return numpy arrays of the same length as the input,
with np.nan padding where data is insufficient.
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
    """Returns (macd_line, signal_line, histogram)."""
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
    """Returns (upper, middle, lower)."""
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
    """Returns (%K, %D) both in [0, 1]."""
    rsi_vals = rsi(prices, rsi_period)
    n        = len(rsi_vals)
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
    """Returns (ADX, +DI, -DI)."""
    H, L, C = _to(highs), _to(lows), _to(closes)
    n = len(C)
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
    adx_vals  = np.full(n, np.nan)
    if n > 2 * period:
        adx_vals[2*period] = float(np.mean(dx[period : 2*period+1]))
        for i in range(2*period+1, n):
            adx_vals[i] = (adx_vals[i-1] * (period-1) + dx[i]) / period
    return adx_vals, plus_di, minus_di


# ─── Helper: find divergence ──────────────────────────────────────────────────

def find_rsi_divergence(closes: np.ndarray, rsi_vals: np.ndarray,
                        lookback: int = 20) -> int:
    """
    Returns:
      +1 if bullish regular divergence (price lower low, RSI higher low)
      -1 if bearish regular divergence (price higher high, RSI lower high)
       0 if no divergence
    """
    n = len(closes)
    if n < lookback + 2:
        return 0

    recent_c   = closes[-lookback:]
    recent_r   = rsi_vals[-lookback:]
    valid_mask = ~np.isnan(recent_r)
    if np.sum(valid_mask) < 4:
        return 0

    # Bullish divergence: price makes lower low, RSI makes higher low
    price_min_idx = int(np.argmin(recent_c))
    rsi_at_min    = recent_r[price_min_idx] if not np.isnan(recent_r[price_min_idx]) else np.nan

    prev_c = closes[-lookback*2 : -lookback] if n >= lookback*2 else closes[:max(1,n-lookback)]
    prev_r = rsi_vals[-lookback*2 : -lookback] if n >= lookback*2 else rsi_vals[:max(1,n-lookback)]
    if len(prev_c) == 0:
        return 0

    prev_min_c   = float(np.min(prev_c))
    prev_min_idx = int(np.argmin(prev_c))
    prev_min_r   = prev_r[prev_min_idx] if not np.isnan(prev_r[prev_min_idx]) else np.nan

    if np.isnan(rsi_at_min) or np.isnan(prev_min_r):
        return 0

    current_min_c = float(recent_c[price_min_idx])
    if current_min_c < prev_min_c and rsi_at_min > prev_min_r:
        strength = abs(rsi_at_min - prev_min_r) / (abs(current_min_c - prev_min_c) + 1e-10)
        if strength > 0.3:
            return 1  # bullish divergence

    # Bearish divergence: price makes higher high, RSI makes lower high
    price_max_idx = int(np.argmax(recent_c))
    rsi_at_max    = recent_r[price_max_idx] if not np.isnan(recent_r[price_max_idx]) else np.nan
    prev_max_c    = float(np.max(prev_c))
    prev_max_idx  = int(np.argmax(prev_c))
    prev_max_r    = prev_r[prev_max_idx] if not np.isnan(prev_r[prev_max_idx]) else np.nan

    if np.isnan(rsi_at_max) or np.isnan(prev_max_r):
        return 0

    current_max_c = float(recent_c[price_max_idx])
    if current_max_c > prev_max_c and rsi_at_max < prev_max_r:
        strength = abs(rsi_at_max - prev_max_r) / (abs(current_max_c - prev_max_c) + 1e-10)
        if strength > 0.3:
            return -1  # bearish divergence

    return 0

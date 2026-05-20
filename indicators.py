"""
indicators.py – Pure-numpy implementations of every indicator used by SIFM.

No TA-Lib, no pandas-ta – works on Render's free tier out of the box.
All functions return numpy arrays of the same length as the input,
with neutral-value padding (0.0 or 50.0 as appropriate) where data is
insufficient.  NaN is never propagated into vote logic.

Architectural contract (enforced throughout):
  • Accept series as short as 20 bars without returning NaN.
  • Return 0 or a neutral value when data is insufficient — never NaN.
  • Never raise exceptions — catch all internally, return neutral on error.
  • All inputs are plain Python lists or numpy arrays — both are handled.
  • All outputs are numpy arrays unless the function explicitly returns a scalar.

v3 changes vs v2:
  • ema          – returns array filled with closes[-1] when too short.
  • rsi          – returns array of 50.0 when too short (no signal bias).
  • macd         – minimum bars = slow + signal; returns zero arrays when too short.
  • stochastic   – NEW function: standard %K/%D; last 5 values guaranteed NaN-free
                   for series ≥ 20 bars; crossing logic at 25/75.
  • find_rsi_divergence – full pivot-based algorithm (swing low / swing high),
                   slope fallback retained, returns 0 on any error.
  • stoch_rsi    – returns zero arrays when too short.
  • adx          – returns zero arrays when too short.
  • All functions wrapped in top-level try/except → return neutral on any error.
"""

import numpy as np
from typing import List, Tuple, Union

ArrayLike = Union[List[float], np.ndarray]


def _to(data: ArrayLike) -> np.ndarray:
    """Convert any array-like to a float64 numpy array."""
    try:
        return np.asarray(data, dtype=float)
    except Exception:
        return np.array([], dtype=float)


# ─── Moving Averages ──────────────────────────────────────────────────────────

def sma(prices: ArrayLike, period: int) -> np.ndarray:
    """
    Simple Moving Average.
    Returns neutral (last close) where insufficient data exists.
    """
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([], dtype=float)
        out = np.full(n, p[-1])          # neutral fill = last close
        for i in range(period - 1, n):
            out[i] = float(np.mean(p[i - period + 1 : i + 1]))
        return out
    except Exception:
        p = _to(prices)
        return np.full(len(p), p[-1] if len(p) > 0 else 0.0)


def ema(prices: ArrayLike, period: int) -> np.ndarray:
    """
    Exponential Moving Average.
    Minimum bars = period.
    Returns array filled with closes[-1] (neutral) when too short.
    """
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([], dtype=float)
        neutral = float(p[-1])
        out = np.full(n, neutral)
        if n < period:
            return out                   # too short → neutral fill
        k = 2.0 / (period + 1)
        out[period - 1] = float(np.mean(p[:period]))
        for i in range(period, n):
            out[i] = float(p[i]) * k + out[i - 1] * (1.0 - k)
        return out
    except Exception:
        p = _to(prices)
        return np.full(len(p), p[-1] if len(p) > 0 else 0.0)


# ─── RSI ─────────────────────────────────────────────────────────────────────

def rsi(prices: ArrayLike, period: int = 14) -> np.ndarray:
    """
    Relative Strength Index.
    Minimum bars = period + 1.
    Returns array of 50.0 when too short (neutral – no bullish/bearish bias).
    """
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([], dtype=float)
        if n < period + 1:
            return np.full(n, 50.0)      # neutral: 50.0
        out = np.full(n, 50.0)
        delta  = np.diff(p)
        gains  = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        avg_g  = float(np.mean(gains[:period]))
        avg_l  = float(np.mean(losses[:period]))
        # seed the first valid RSI value
        if avg_l == 0.0:
            out[period] = 100.0
        else:
            out[period] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
        for i in range(period, len(delta)):
            avg_g = (avg_g * (period - 1) + gains[i])  / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
            if avg_l == 0.0:
                out[i + 1] = 100.0
            else:
                out[i + 1] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
        return out
    except Exception:
        p = _to(prices)
        return np.full(len(p), 50.0)


# ─── MACD ────────────────────────────────────────────────────────────────────

def macd(
    prices: ArrayLike,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    MACD line, signal line, histogram.
    Minimum bars = slow + signal.
    Returns (zeros, zeros, zeros) when too short.
    """
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([]), np.array([]), np.array([])
        if n < slow + signal:
            z = np.zeros(n)
            return z.copy(), z.copy(), z.copy()

        fast_ema  = ema(p, fast)
        slow_ema  = ema(p, slow)
        macd_line = fast_ema - slow_ema

        sig_line = np.zeros(n)
        # Find where macd_line is meaningful (after slow EMA seeds)
        start = slow - 1
        if n - start >= signal:
            sig_vals = ema(macd_line[start:], signal)
            sig_line[start:] = sig_vals

        hist = macd_line - sig_line
        return macd_line, sig_line, hist
    except Exception:
        p = _to(prices)
        z = np.zeros(len(p))
        return z.copy(), z.copy(), z.copy()


# ─── Bollinger Bands ─────────────────────────────────────────────────────────

def bollinger_bands(
    prices: ArrayLike,
    period: int = 20,
    std_dev: float = 2.0,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Bollinger Bands: (upper, mid, lower).
    Neutral fill uses last close for upper/lower and mid.
    """
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([]), np.array([]), np.array([])
        neutral = float(p[-1])
        mid   = sma(p, period)
        std   = np.full(n, 0.0)
        for i in range(period - 1, n):
            std[i] = float(np.std(p[i - period + 1 : i + 1], ddof=0))
        upper = mid + std_dev * std
        lower = mid - std_dev * std
        # Replace any remaining nan with neutral
        upper = np.where(np.isnan(upper), neutral, upper)
        mid   = np.where(np.isnan(mid),   neutral, mid)
        lower = np.where(np.isnan(lower), neutral, lower)
        return upper, mid, lower
    except Exception:
        p = _to(prices)
        neutral = float(p[-1]) if len(p) > 0 else 0.0
        arr = np.full(len(p), neutral)
        return arr.copy(), arr.copy(), arr.copy()


# ─── Stochastic Oscillator ────────────────────────────────────────────────────

def stochastic(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    k: int = 14,
    d: int = 3,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Standard Stochastic Oscillator.
    Returns (%K, %D), both in [0, 100].

    Crossing logic (caller-side):
        prev_k = K[-2]; curr_k = K[-1]
        bullish_cross = prev_k < 25 and curr_k >= 25   # crossing up through 25
        bearish_cross = prev_k > 75 and curr_k <= 75   # crossing down through 75

    Minimum bars = k + d (≈17 for defaults).
    Last 5 values are guaranteed NaN-free for series ≥ 20 bars.
    Returns (50-filled, 50-filled) when too short.
    """
    try:
        H = _to(highs)
        L = _to(lows)
        C = _to(closes)
        n = len(C)
        if n == 0:
            return np.array([]), np.array([])

        neutral_K = np.full(n, 50.0)
        neutral_D = np.full(n, 50.0)

        if n < k + d:
            return neutral_K, neutral_D

        raw_k = np.full(n, 50.0)
        for i in range(k - 1, n):
            highest = float(np.max(H[i - k + 1 : i + 1]))
            lowest  = float(np.min(L[i - k + 1 : i + 1]))
            if highest == lowest:
                raw_k[i] = 50.0
            else:
                raw_k[i] = 100.0 * (float(C[i]) - lowest) / (highest - lowest)

        # %D = SMA(k_period) of raw %K
        K_out = raw_k.copy()
        D_out = np.full(n, 50.0)
        for i in range(d - 1, n):
            D_out[i] = float(np.mean(K_out[i - d + 1 : i + 1]))

        # Guarantee last 5 values are NaN-free (they're floats so nan can't creep
        # in through our logic, but guard against any edge case explicitly)
        K_out[-5:] = np.where(np.isnan(K_out[-5:]), 50.0, K_out[-5:])
        D_out[-5:] = np.where(np.isnan(D_out[-5:]), 50.0, D_out[-5:])

        return K_out, D_out
    except Exception:
        C = _to(closes)
        n = len(C)
        return np.full(n, 50.0), np.full(n, 50.0)


# ─── ATR ─────────────────────────────────────────────────────────────────────

def atr(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    period: int = 14,
) -> np.ndarray:
    """
    Average True Range.
    Returns zero array when too short.
    """
    try:
        H, L, C = _to(highs), _to(lows), _to(closes)
        n = len(C)
        if n == 0:
            return np.array([], dtype=float)
        tr = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(
                H[i] - L[i],
                abs(H[i] - C[i - 1]),
                abs(L[i] - C[i - 1]),
            )
        out = np.zeros(n)
        if n > period:
            out[period] = float(np.mean(tr[1 : period + 1]))
            for i in range(period + 1, n):
                out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
        return out
    except Exception:
        C = _to(closes)
        return np.zeros(len(C))


# ─── ADX ─────────────────────────────────────────────────────────────────────

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
            up   = H[i] - H[i - 1]
            down = L[i - 1] - L[i]
            plus_dm[i]  = up   if (up > down   and up > 0)   else 0.0
            minus_dm[i] = down if (down > up   and down > 0) else 0.0
            tr_arr[i]   = max(
                H[i] - L[i],
                abs(H[i] - C[i - 1]),
                abs(L[i] - C[i - 1]),
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


# ─── Stochastic RSI ──────────────────────────────────────────────────────────

def stoch_rsi(
    prices: ArrayLike,
    rsi_period: int = 14,
    stoch_period: int = 14,
    k: int = 3,
    d: int = 3,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Stochastic RSI.
    Returns (%K, %D), both in [0, 1].
    Returns (zeros, zeros) when too short, preventing downstream NaN errors.
    """
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([]), np.array([])

        min_len = rsi_period + stoch_period + k + d
        if n < min_len:
            return np.zeros(n), np.zeros(n)

        rsi_vals = rsi(p, rsi_period)
        raw_k = np.full(n, 0.5)
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
        p = _to(prices)
        return np.zeros(len(p)), np.zeros(len(p))


# ─── RSI Divergence ───────────────────────────────────────────────────────────

# Minimum slope magnitude for slope-fallback divergence detection.
_SLOPE_MIN_THRESHOLD = 0.01  # RSI points per bar (normalised)


def find_rsi_divergence(
    closes: ArrayLike,
    rsi_values: ArrayLike,
    lookback: int = 20,
) -> int:
    """
    Pivot-based RSI divergence detection with slope fallback.

    Returns:
      +1  bullish divergence  (price lower low,  RSI higher low)
      -1  bearish divergence  (price higher high, RSI lower high)
       0  no divergence or insufficient data

    Algorithm:
      1. Find the last 2 swing lows in `closes` within the lookback window
         (local minima: close[i] < close[i-1] and close[i] < close[i+1]).
      2. If swing_low_2 < swing_low_1 AND rsi[swing_low_2] > rsi[swing_low_1]
         → bullish divergence (+1).
      3. Find the last 2 swing highs in `closes` within the lookback window
         (local maxima: close[i] > close[i-1] and close[i] > close[i+1]).
      4. If swing_high_2 > swing_high_1 AND rsi[swing_high_2] < rsi[swing_high_1]
         → bearish divergence (-1).
      5. Else slope fallback → +1 / -1 / 0.
      6. Returns 0 on any error or < 20 bars.
    """
    try:
        closes    = _to(closes)
        rsi_vals  = _to(rsi_values)

        n = len(closes)
        if n < 20 or n < lookback:
            return 0

        # Work within the lookback window
        lb   = min(lookback, n)
        c_w  = closes[-lb:]
        r_w  = rsi_vals[-lb:]
        m    = len(c_w)

        # ── Find swing lows (local minima) ────────────────────────────────────
        swing_low_indices = []
        for i in range(1, m - 1):
            if c_w[i] < c_w[i - 1] and c_w[i] < c_w[i + 1]:
                swing_low_indices.append(i)

        if len(swing_low_indices) >= 2:
            idx1 = swing_low_indices[-2]   # older swing low
            idx2 = swing_low_indices[-1]   # more recent swing low

            rsi1 = float(r_w[idx1])
            rsi2 = float(r_w[idx2])
            c1   = float(c_w[idx1])
            c2   = float(c_w[idx2])

            if not (np.isnan(rsi1) or np.isnan(rsi2)):
                # Bullish divergence: price made lower low, RSI made higher low
                if c2 < c1 and rsi2 > rsi1:
                    return 1

        # ── Find swing highs (local maxima) ───────────────────────────────────
        swing_high_indices = []
        for i in range(1, m - 1):
            if c_w[i] > c_w[i - 1] and c_w[i] > c_w[i + 1]:
                swing_high_indices.append(i)

        if len(swing_high_indices) >= 2:
            idx1 = swing_high_indices[-2]   # older swing high
            idx2 = swing_high_indices[-1]   # more recent swing high

            rsi1 = float(r_w[idx1])
            rsi2 = float(r_w[idx2])
            c1   = float(c_w[idx1])
            c2   = float(c_w[idx2])

            if not (np.isnan(rsi1) or np.isnan(rsi2)):
                # Bearish divergence: price made higher high, RSI made lower high
                if c2 > c1 and rsi2 < rsi1:
                    return -1

        # ── Slope fallback ────────────────────────────────────────────────────
        return _slope_divergence(closes, rsi_vals, lookback)

    except Exception:
        return 0


def _slope_divergence(
    closes: np.ndarray,
    rsi_vals: np.ndarray,
    lookback: int,
) -> int:
    """
    Slope-based divergence fallback using linear regression over the last
    `lookback` bars.

    Returns +1 (bullish), -1 (bearish), or 0.
    Never raises.
    """
    try:
        n  = len(closes)
        lb = min(lookback, n)
        if lb < 4:
            return 0

        c_window = closes[-lb:].astype(float)
        r_window = rsi_vals[-lb:].astype(float)

        valid = ~np.isnan(r_window)
        if np.sum(valid) < 4:
            return 0

        x = np.arange(lb, dtype=float)

        mean_c = float(np.mean(c_window))
        if mean_c == 0.0:
            mean_c = 1.0
        c_poly      = np.polyfit(x, c_window / mean_c, 1)
        price_slope = c_poly[0] * lb  # total normalised change over window

        r_poly    = np.polyfit(x[valid], r_window[valid], 1)
        rsi_slope = r_poly[0] * lb    # total RSI change over window

        thresh = _SLOPE_MIN_THRESHOLD

        if price_slope < -thresh and rsi_slope > thresh:
            return 1   # bullish: price falling, RSI rising

        if price_slope > thresh and rsi_slope < -thresh:
            return -1  # bearish: price rising, RSI falling

        return 0
    except Exception:
        return 0

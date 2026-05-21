"""
indicators.py – Pure-numpy indicator implementations for the Deriv Trading Bot.
v4 — Rebuilt for Range Break + Boom/Crash strategies only.

Contracts:
  • Accept series as short as 20 bars without returning NaN.
  • Return 0 or a neutral value when data is insufficient — never NaN.
  • Never raise exceptions — catch all internally, return neutral on error.
  • All inputs are plain Python lists or numpy arrays — both handled.
  • All outputs are numpy arrays unless explicitly documented otherwise.
"""

import numpy as np
from typing import List, Tuple, Union

ArrayLike = Union[List[float], np.ndarray]


def _to(data: ArrayLike) -> np.ndarray:
    try:
        return np.asarray(data, dtype=float)
    except Exception:
        return np.array([], dtype=float)


# ─── EMA ─────────────────────────────────────────────────────────────────────

def ema(prices: ArrayLike, period: int) -> np.ndarray:
    """
    Exponential Moving Average.
    Returns array filled with closes[-1] (neutral) when series < period.
    """
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([], dtype=float)
        neutral = float(p[-1])
        out = np.full(n, neutral)
        if n < period:
            return out
        k = 2.0 / (period + 1)
        out[period - 1] = float(np.mean(p[:period]))
        for i in range(period, n):
            out[i] = float(p[i]) * k + out[i - 1] * (1.0 - k)
        return out
    except Exception:
        p = _to(prices)
        return np.full(len(p), float(p[-1]) if len(p) > 0 else 0.0)


def sma(prices: ArrayLike, period: int) -> np.ndarray:
    """Simple Moving Average. Neutral fill = last close."""
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([], dtype=float)
        out = np.full(n, float(p[-1]))
        for i in range(period - 1, n):
            out[i] = float(np.mean(p[i - period + 1:i + 1]))
        return out
    except Exception:
        p = _to(prices)
        return np.full(len(p), float(p[-1]) if len(p) > 0 else 0.0)


# ─── RSI ─────────────────────────────────────────────────────────────────────

def rsi(prices: ArrayLike, period: int = 14) -> np.ndarray:
    """
    Standard RSI.
    Returns array of 50.0 (neutral) when series < period+1.
    No NaN in last 5 values for series >= 20.
    """
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([], dtype=float)
        if n < period + 1:
            return np.full(n, 50.0)
        out = np.full(n, 50.0)
        delta  = np.diff(p)
        gains  = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        avg_g  = float(np.mean(gains[:period]))
        avg_l  = float(np.mean(losses[:period]))
        if avg_l == 0.0:
            out[period] = 100.0
        else:
            out[period] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
        for i in range(period, len(delta)):
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
            if avg_l == 0.0:
                out[i + 1] = 100.0
            else:
                out[i + 1] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
        # Guarantee last 5 are NaN-free
        out[-5:] = np.where(np.isnan(out[-5:]), 50.0, out[-5:])
        return out
    except Exception:
        p = _to(prices)
        return np.full(len(p), 50.0)


# ─── ATR ─────────────────────────────────────────────────────────────────────

def atr(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
        period: int = 14) -> np.ndarray:
    """
    Average True Range using Wilder smoothing.
    No NaN for series >= 15. Returns zeros for insufficient data.
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
            out[period] = float(np.mean(tr[1:period + 1]))
            for i in range(period + 1, n):
                out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
        return out
    except Exception:
        C = _to(closes)
        return np.zeros(len(C))


# ─── Bollinger Bands ─────────────────────────────────────────────────────────

def bollinger_bands(prices: ArrayLike, period: int = 20,
                    std_dev: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Bollinger Bands: (upper, mid, lower).
    Neutral fill uses last close. Never raises.
    """
    try:
        p = _to(prices)
        n = len(p)
        if n == 0:
            return np.array([]), np.array([]), np.array([])
        neutral = float(p[-1])
        mid = sma(p, period)
        std = np.zeros(n)
        for i in range(period - 1, n):
            std[i] = float(np.std(p[i - period + 1:i + 1], ddof=0))
        upper = mid + std_dev * std
        lower = mid - std_dev * std
        upper = np.where(np.isnan(upper), neutral, upper)
        mid   = np.where(np.isnan(mid),   neutral, mid)
        lower = np.where(np.isnan(lower), neutral, lower)
        return upper, mid, lower
    except Exception:
        p = _to(prices)
        neutral = float(p[-1]) if len(p) > 0 else 0.0
        arr = np.full(len(p), neutral)
        return arr.copy(), arr.copy(), arr.copy()


# ─── MACD (retained for compatibility) ───────────────────────────────────────

def macd(prices: ArrayLike, fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
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
        sig_line  = np.zeros(n)
        start = slow - 1
        if n - start >= signal:
            sig_line[start:] = ema(macd_line[start:], signal)
        hist = macd_line - sig_line
        return macd_line, sig_line, hist
    except Exception:
        p = _to(prices)
        z = np.zeros(len(p))
        return z.copy(), z.copy(), z.copy()


# ─── Stochastic (retained for compatibility) ──────────────────────────────────

def stochastic(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
               k: int = 14, d: int = 3) -> Tuple[np.ndarray, np.ndarray]:
    try:
        H, L, C = _to(highs), _to(lows), _to(closes)
        n = len(C)
        if n == 0:
            return np.array([]), np.array([])
        if n < k + d:
            return np.full(n, 50.0), np.full(n, 50.0)
        raw_k = np.full(n, 50.0)
        for i in range(k - 1, n):
            highest = float(np.max(H[i - k + 1:i + 1]))
            lowest  = float(np.min(L[i - k + 1:i + 1]))
            if highest == lowest:
                raw_k[i] = 50.0
            else:
                raw_k[i] = 100.0 * (float(C[i]) - lowest) / (highest - lowest)
        K_out = raw_k.copy()
        D_out = np.full(n, 50.0)
        for i in range(d - 1, n):
            D_out[i] = float(np.mean(K_out[i - d + 1:i + 1]))
        K_out[-5:] = np.where(np.isnan(K_out[-5:]), 50.0, K_out[-5:])
        D_out[-5:] = np.where(np.isnan(D_out[-5:]), 50.0, D_out[-5:])
        return K_out, D_out
    except Exception:
        C = _to(closes)
        n = len(C)
        return np.full(n, 50.0), np.full(n, 50.0)


def adx(highs: ArrayLike, lows: ArrayLike, closes: ArrayLike,
        period: int = 14) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
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
            plus_dm[i]  = up   if (up > down and up > 0)   else 0.0
            minus_dm[i] = down if (down > up and down > 0) else 0.0
            tr_arr[i]   = max(H[i] - L[i], abs(H[i] - C[i-1]), abs(L[i] - C[i-1]))

        def _wilder(arr, p):
            out = np.zeros(n)
            out[p] = float(np.sum(arr[1:p + 1]))
            for i in range(p + 1, n):
                out[i] = out[i-1] - out[i-1] / p + arr[i]
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
            dx = np.where(denom != 0, 100.0 * np.abs(plus_di - minus_di) / safe_denom, 0.0)
        adx_vals = np.zeros(n)
        if n > 2 * period:
            adx_vals[2 * period] = float(np.mean(dx[period:2 * period + 1]))
            for i in range(2 * period + 1, n):
                adx_vals[i] = (adx_vals[i-1] * (period - 1) + dx[i]) / period
        return adx_vals, plus_di, minus_di
    except Exception:
        C = _to(closes)
        n = len(C)
        return np.zeros(n), np.zeros(n), np.zeros(n)

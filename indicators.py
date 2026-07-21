"""
indicators.py – Pure-numpy trading indicators for Deriv SIFM bot.

Architectural contract (enforced throughout):
  • Every function handles series as short as 15 bars without NaN.
  • Never raises exceptions — all internal errors are caught and a
    neutral/safe value is returned instead.
  • Never returns None.
  • Accepts plain Python lists or numpy arrays.
  • All outputs are numpy arrays (or scalars/tuples where documented).
"""

import numpy as np
from typing import List, Optional, Tuple, Union

ArrayLike = Union[List[float], np.ndarray]


# ─── Internal helpers ──────────────────────────────────────────────────────

def _to(data: ArrayLike) -> np.ndarray:
    """Convert any array-like to a float64 numpy array, silently."""
    try:
        arr = np.asarray(data, dtype=np.float64).flatten()
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        return arr
    except Exception:
        return np.array([], dtype=np.float64)


def _safe_last(arr: np.ndarray, fallback: float = 0.0) -> float:
    try:
        return float(arr[-1]) if len(arr) > 0 else fallback
    except Exception:
        return fallback


def _fill(n: int, value: float) -> np.ndarray:
    try:
        return np.full(max(n, 0), value, dtype=np.float64)
    except Exception:
        return np.array([], dtype=np.float64)


def _sma(data: np.ndarray, period: int) -> np.ndarray:
    """Growing-window SMA — no NaN, same length as input."""
    n = len(data)
    if n == 0:
        return np.array([], dtype=np.float64)
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        start = max(0, i - period + 1)
        out[i] = float(np.mean(data[start:i + 1]))
    return out


# ─── EMA ────────────────────────────────────────────────────────────────────

def ema(closes: ArrayLike, period: int) -> np.ndarray:
    """
    Standard EMA. Returns array same length as closes.
    For series shorter than period, fills with growing-window SMA.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([], dtype=np.float64)
        period = max(int(period), 1)

        if n < period:
            return _sma(p, period)

        out = np.empty(n, dtype=np.float64)
        k = 2.0 / (period + 1.0)
        # Back-fill pre-seed positions with growing-window SMA
        for i in range(period - 1):
            out[i] = float(np.mean(p[:i + 1]))
        out[period - 1] = float(np.mean(p[:period]))
        for i in range(period, n):
            out[i] = float(p[i]) * k + out[i - 1] * (1.0 - k)
        return out
    except Exception:
        p = _to(closes)
        return _fill(len(p), _safe_last(p))


# ─── RSI ────────────────────────────────────────────────────────────────────

def rsi(closes: ArrayLike, period: int = 14) -> np.ndarray:
    """
    Standard RSI 0-100. Returns array same length as closes.
    Minimum 15 bars required; returns array of 50s (neutral) otherwise.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([], dtype=np.float64)
        if n < 15:
            return _fill(n, 50.0)

        out = _fill(n, 50.0)
        delta = np.diff(p)
        gains = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)

        avg_g = float(np.mean(gains[:period]))
        avg_l = float(np.mean(losses[:period]))

        def _val(ag: float, al: float) -> float:
            if al == 0.0:
                return 100.0 if ag > 0 else 50.0
            return 100.0 - 100.0 / (1.0 + ag / al)

        out[period] = _val(avg_g, avg_l)
        for i in range(period, len(delta)):
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
            out[i + 1] = _val(avg_g, avg_l)
        return out
    except Exception:
        p = _to(closes)
        return _fill(len(p), 50.0)


# ─── MACD ───────────────────────────────────────────────────────────────────

def macd(
    closes: ArrayLike,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns (macd_line, signal_line, histogram) arrays, same length as input.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return (np.array([], dtype=np.float64),) * 3

        fast_ema = ema(p, fast)
        slow_ema = ema(p, slow)
        macd_line = fast_ema - slow_ema
        sig_line = ema(macd_line, signal)
        hist = macd_line - sig_line
        return macd_line, sig_line, hist
    except Exception:
        p = _to(closes)
        z = _fill(len(p), 0.0)
        return z.copy(), z.copy(), z.copy()


# ─── Bollinger Bands ────────────────────────────────────────────────────────

def bollinger_bands(
    closes: ArrayLike,
    period: int = 20,
    std_dev: float = 2.0,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns (upper, mid, lower) arrays, same length as closes.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return (np.array([], dtype=np.float64),) * 3

        mid = _sma(p, period)
        std = np.empty(n, dtype=np.float64)
        for i in range(n):
            start = max(0, i - period + 1)
            std[i] = float(np.std(p[start:i + 1], ddof=0))

        upper = mid + std_dev * std
        lower = mid - std_dev * std
        return upper, mid, lower
    except Exception:
        p = _to(closes)
        neutral = _safe_last(p)
        a = _fill(len(p), neutral)
        return a.copy(), a.copy(), a.copy()


# ─── ATR ────────────────────────────────────────────────────────────────────

def atr(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    period: int = 14,
) -> np.ndarray:
    """
    Average True Range (Wilder smoothing). Returns array same length as closes.
    No NaN for series >= 15 bars.
    """
    try:
        H, L, C = _to(highs), _to(lows), _to(closes)
        n = len(C)
        if n == 0:
            return np.array([], dtype=np.float64)
        if len(H) != n:
            H = C.copy()
        if len(L) != n:
            L = C.copy()

        tr = np.empty(n, dtype=np.float64)
        tr[0] = float(H[0]) - float(L[0])
        for i in range(1, n):
            tr[i] = max(
                float(H[i]) - float(L[i]),
                abs(float(H[i]) - float(C[i - 1])),
                abs(float(L[i]) - float(C[i - 1])),
            )

        out = np.empty(n, dtype=np.float64)
        if n < period:
            out = _sma(tr, period)
            return out

        out[period - 1] = float(np.mean(tr[:period]))
        for i in range(period - 1):
            out[i] = float(np.mean(tr[:i + 1]))
        for i in range(period, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
        return out
    except Exception:
        C = _to(closes)
        return _fill(len(C), 0.0)


# ─── Stochastic Oscillator ──────────────────────────────────────────────────

def stochastic(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    k_period: int = 14,
    d_period: int = 3,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Returns (%K, %D) arrays, 0-100, same length as closes.
    %K crossing up from below 25 = bullish.
    """
    try:
        H, L, C = _to(highs), _to(lows), _to(closes)
        n = len(C)
        if n == 0:
            return np.array([], dtype=np.float64), np.array([], dtype=np.float64)
        if len(H) != n:
            H = C.copy()
        if len(L) != n:
            L = C.copy()

        k_raw = _fill(n, 50.0)
        for i in range(n):
            start = max(0, i - k_period + 1)
            hh = float(np.max(H[start:i + 1]))
            ll = float(np.min(L[start:i + 1]))
            if hh == ll:
                k_raw[i] = 50.0
            else:
                k_raw[i] = 100.0 * (float(C[i]) - ll) / (hh - ll)

        d = _sma(k_raw, d_period)
        return k_raw, d
    except Exception:
        C = _to(closes)
        n = len(C)
        return _fill(n, 50.0), _fill(n, 50.0)


# ─── Rate of Change ─────────────────────────────────────────────────────────

def rate_of_change(closes: ArrayLike, period: int = 10) -> np.ndarray:
    """
    ROC = (close - close[n periods ago]) / close[n periods ago]
    Returns array same length as closes. Early bars use largest available lag.
    """
    try:
        p = _to(closes)
        n = len(p)
        if n == 0:
            return np.array([], dtype=np.float64)

        out = np.zeros(n, dtype=np.float64)
        for i in range(n):
            lag = min(period, i)
            ref = float(p[i - lag]) if lag > 0 else float(p[i])
            if ref == 0.0:
                out[i] = 0.0
            else:
                out[i] = (float(p[i]) - ref) / ref
        return out
    except Exception:
        p = _to(closes)
        return _fill(len(p), 0.0)


# ─── Donchian Channel ───────────────────────────────────────────────────────

def donchian_channel(
    highs: ArrayLike,
    lows: ArrayLike,
    period: int = 20,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns (upper, lower, mid) arrays, same length as highs.
    Upper = highest high, Lower = lowest low over period.
    """
    try:
        H, L = _to(highs), _to(lows)
        n = len(H)
        if n == 0:
            return (np.array([], dtype=np.float64),) * 3
        if len(L) != n:
            L = H.copy()

        upper = np.empty(n, dtype=np.float64)
        lower = np.empty(n, dtype=np.float64)
        for i in range(n):
            start = max(0, i - period + 1)
            upper[i] = float(np.max(H[start:i + 1]))
            lower[i] = float(np.min(L[start:i + 1]))
        mid = (upper + lower) / 2.0
        return upper, lower, mid
    except Exception:
        H = _to(highs)
        neutral = _safe_last(H)
        a = _fill(len(H), neutral)
        return a.copy(), a.copy(), a.copy()


# ─── Consolidation Detector ─────────────────────────────────────────────────

def find_consolidation(
    highs: ArrayLike,
    lows: ArrayLike,
    closes: ArrayLike,
    lookback: int = 15,
    avg_lookback: int = 50,
    ratio: float = 0.4,
) -> Optional[Tuple[float, float]]:
    """
    Returns (upper, lower) bounds of the current consolidation zone if the
    recent lookback range is tight (< ratio * avg_lookback range), else None.

    Used for Range Break strategy. signal_engine.py calls this as
    find_consolidation(H, L, C) and does:
        consolidation = ind.find_consolidation(H, L, C)
        has_consolidation = consolidation is not None
        if has_consolidation:
            cons_upper, cons_lower = consolidation
    — so the argument order and None/tuple return here must match that.
    """
    try:
        H, L, C = _to(highs), _to(lows), _to(closes)
        n = len(C)
        if len(H) != n:
            H = C.copy()
        if len(L) != n:
            L = C.copy()
        if n < lookback:
            return None

        recent_start = max(0, n - lookback)
        recent_high  = float(np.max(H[recent_start:n]))
        recent_low   = float(np.min(L[recent_start:n]))
        recent_range = recent_high - recent_low

        avg_start    = max(0, n - avg_lookback)
        avg_window_h = H[avg_start:n]
        avg_window_l = L[avg_start:n]
        if len(avg_window_h) == 0:
            return None
        avg_range = float(np.max(avg_window_h) - np.min(avg_window_l))

        if avg_range == 0.0:
            return None
        if recent_range < ratio * avg_range:
            return (recent_high, recent_low)
        return None
    except Exception:
        return None


# ─── Spike Detector ─────────────────────────────────────────────────────────

def detect_spike(
    closes: ArrayLike,
    highs: ArrayLike,
    lows: ArrayLike,
    period: int = 14,
    atr_multiplier: float = 3.0,
) -> int:
    """
    Returns +1 (upward spike), -1 (downward spike), 0 (none).
    Spike = single bar (last bar) moves more than atr_multiplier * ATR(period).
    """
    try:
        C = _to(closes)
        H = _to(highs)
        L = _to(lows)
        n = len(C)
        if n < 2:
            return 0
        if len(H) != n:
            H = C.copy()
        if len(L) != n:
            L = C.copy()

        atr_vals = atr(H, L, C, period)
        current_atr = _safe_last(atr_vals, 0.0)
        if current_atr <= 0.0:
            return 0

        move = float(C[-1]) - float(C[-2])
        threshold = atr_multiplier * current_atr
        if abs(move) > threshold:
            return 1 if move > 0 else -1
        return 0
    except Exception:
        return 0


# ─── Digit Over/Under Score ─────────────────────────────────────────────────

def digit_score(
    closes: ArrayLike,
    rsi_vals: ArrayLike,
    bb_upper: ArrayLike,
    bb_lower: ArrayLike,
    roc_vals: ArrayLike,
) -> Tuple[int, str]:
    """
    Scoring system for digit over/under strategy, using the latest values
    of each input series. Returns (score, direction) where
    direction = "OVER" | "UNDER" | "NONE". Minimum score 6 required.
    """
    try:
        C = _to(closes)
        R = _to(rsi_vals)
        BU = _to(bb_upper)
        BL = _to(bb_lower)
        ROC = _to(roc_vals)

        if len(C) == 0 or len(R) == 0 or len(BU) == 0 or len(BL) == 0 or len(ROC) == 0:
            return 0, "NONE"

        close_v = _safe_last(C)
        rsi_v = _safe_last(R, 50.0)
        bbu_v = _safe_last(BU, close_v)
        bbl_v = _safe_last(BL, close_v)
        roc_v = _safe_last(ROC, 0.0)

        over_score = 0
        under_score = 0

        if rsi_v > 78:
            under_score += 3
        if rsi_v < 22:
            over_score += 3
        if close_v >= bbu_v:
            under_score += 3
        if close_v <= bbl_v:
            over_score += 3
        if roc_v < -0.02:
            over_score += 2
        if roc_v > 0.02:
            under_score += 2

        if over_score >= 6 and over_score > under_score:
            return over_score, "OVER"
        if under_score >= 6 and under_score > over_score:
            return under_score, "UNDER"
        return max(over_score, under_score), "NONE"
    except Exception:
        return 0, "NONE"


# ─── RSI Divergence ─────────────────────────────────────────────────────────

def find_rsi_divergence(
    closes: ArrayLike,
    rsi_vals: ArrayLike,
    lookback: int = 20,
) -> int:
    """
    Bullish divergence (price lower low, RSI higher low): returns +1
    Bearish divergence (price higher high, RSI lower high): returns -1
    None: returns 0. Works on a minimum of 20 bars.
    """
    try:
        C = _to(closes)
        R = _to(rsi_vals)
        n = min(len(C), len(R))
        if n < lookback or n < 20:
            return 0

        C = C[-lookback:]
        R = R[-lookback:]
        m = len(C)
        half = max(1, m // 2)

        first_c = C[:half]
        second_c = C[half:]
        first_r = R[:half]
        second_r = R[half:]

        if len(second_c) == 0 or len(first_c) == 0:
            return 0

        low1_idx = int(np.argmin(first_c))
        low2_idx = int(np.argmin(second_c))
        price_low1 = float(first_c[low1_idx])
        price_low2 = float(second_c[low2_idx])
        rsi_low1 = float(first_r[low1_idx])
        rsi_low2 = float(second_r[low2_idx])

        if price_low2 < price_low1 and rsi_low2 > rsi_low1:
            return 1

        high1_idx = int(np.argmax(first_c))
        high2_idx = int(np.argmax(second_c))
        price_high1 = float(first_c[high1_idx])
        price_high2 = float(second_c[high2_idx])
        rsi_high1 = float(first_r[high1_idx])
        rsi_high2 = float(second_r[high2_idx])

        if price_high2 > price_high1 and rsi_high2 < rsi_high1:
            return -1

        return 0
    except Exception:
        return 0


# ─── Compatibility aliases ───────────────────────────────────────────────────
# signal_engine.py calls these under the shorter names below. Kept as thin
# wrappers so the original functions/docstrings above stay untouched.

def roc(closes: ArrayLike, period: int = 10) -> np.ndarray:
    """Alias for rate_of_change(), matching the name signal_engine.py calls."""
    return rate_of_change(closes, period)


def donchian(
    highs: ArrayLike,
    lows: ArrayLike,
    period: int = 20,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Alias for donchian_channel(), but returns only (upper, lower) — matching
    the 2-value unpack signal_engine.py does (it doesn't use the mid band).
    """
    upper, lower, _mid = donchian_channel(highs, lows, period)
    return upper, lower

"""
symbol_manager.py – Intelligent symbol rotation with adaptive prioritisation.

Logic:
  • Maintains a performance score per symbol (win-rate, profit, recency).
  • Symbols with consistent losses are temporarily deprioritised.
  • Synthetics (always available) fill the queue when real markets are closed.
  • Market-hours awareness prevents scanning closed instruments.
  • Every N cycles the bot re-queries Deriv's active_symbols list.
"""

import time
import logging
import datetime
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field
import pytz

import symbols as sym_module

logger = logging.getLogger(__name__)

UTC = pytz.utc

# Market session hours in UTC  (open_hour, open_min, close_hour, close_min)
SESSIONS = {
    "sydney":    (21, 0, 6, 0),   # 21:00–06:00 UTC
    "tokyo":     (0,  0, 9, 0),   # 00:00–09:00 UTC
    "london":    (7,  0, 16, 0),  # 07:00–16:00 UTC
    "new_york":  (12, 0, 21, 0),  # 12:00–21:00 UTC
}

# Map Deriv symbol prefix → relevant session(s)
SYMBOL_SESSION: Dict[str, List[str]] = {
    "frxEURUSD": ["london", "new_york"],
    "frxGBPUSD": ["london", "new_york"],
    "frxUSDJPY": ["tokyo",  "new_york"],
    "frxAUDUSD": ["sydney", "london"],
    "frxUSDCAD": ["new_york"],
    "frxUSDCHF": ["london", "new_york"],
    "frxNZDUSD": ["sydney"],
    "frxEURGBP": ["london"],
    "frxEURJPY": ["london", "tokyo"],
    "frxGBPJPY": ["london", "tokyo"],
    "frxXAUUSD": ["london", "new_york"],
    "frxXAGUSD": ["london", "new_york"],
    "frxUSOIL":  ["new_york"],
    "frxUKOIL":  ["london"],
    "cryBTCUSD": [],  # 24/7
    "cryETHUSD": [],  # 24/7
}

# Synthetics are always available
for s in sym_module.SYNTHETIC:
    SYMBOL_SESSION[s] = []


@dataclass
class SymbolStats:
    symbol:        str
    trades:        int   = 0
    wins:          int   = 0
    total_pnl:     float = 0.0
    last_trade_ts: float = 0.0
    cooldown_until: float = 0.0   # epoch – deprioritised until this time

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades) if self.trades > 0 else 0.5

    @property
    def score(self) -> float:
        """Higher = more desirable to trade."""
        recency_bonus = 0.1 if time.time() - self.last_trade_ts > 3600 else 0
        pnl_score     = min(self.total_pnl * 10, 1.0)   # capped at +1
        wr_score      = self.win_rate                     # 0–1
        return wr_score + pnl_score + recency_bonus


class SymbolManager:
    """
    Maintains and rotates the symbol scan queue intelligently.
    """

    def __init__(self,
                 refresh_interval:  int = 3600,   # re-check active symbols hourly
                 cooldown_trades:   int = 3,       # consecutive losses before cooldown
                 cooldown_seconds:  int = 1800):   # 30-minute cooldown
        self._refresh_interval = refresh_interval
        self._cooldown_trades  = cooldown_trades
        self._cooldown_seconds = cooldown_seconds

        self._stats:    Dict[str, SymbolStats] = {}
        self._active:   Set[str] = set(sym_module.SYNTHETIC)  # start with synthetics
        self._last_refresh: float = 0.0
        self._consecutive_losses: Dict[str, int] = {}

        # Initialise stats for all known symbols
        for sym in sym_module.ALL_SYMBOLS:
            self._stats[sym] = SymbolStats(symbol=sym)

    # ── Active symbol list ────────────────────────────────────────────────────

    def update_active(self, active_symbols_from_api: List[str]):
        """Call with the list returned by DerivClient.get_active_symbols()."""
        self._active = set(active_symbols_from_api) | set(sym_module.SYNTHETIC)
        self._last_refresh = time.time()
        logger.info(f"SymbolManager: {len(self._active)} active symbols")

    def needs_refresh(self) -> bool:
        return time.time() - self._last_refresh > self._refresh_interval

    # ── Session hours check ───────────────────────────────────────────────────

    def _in_session(self, symbol: str) -> bool:
        """Return True if this symbol's main market is currently open."""
        sessions = SYMBOL_SESSION.get(symbol, None)
        if sessions is None:
            # Unknown symbol – assume tradeable (synthetics etc.)
            return True
        if sessions == []:
            # 24/7 instrument
            return True

        now_utc = datetime.datetime.utcnow()
        h, m    = now_utc.hour, now_utc.minute
        now_min = h * 60 + m   # minutes since midnight

        for sess_name in sessions:
            oh, om, ch, cm = SESSIONS[sess_name]
            open_min  = oh * 60 + om
            close_min = ch * 60 + cm
            if open_min < close_min:
                # Same-day session
                if open_min <= now_min <= close_min:
                    return True
            else:
                # Overnight session (e.g. Sydney 21:00–06:00)
                if now_min >= open_min or now_min <= close_min:
                    return True
        return False

    # ── Cooldown management ───────────────────────────────────────────────────

    def _apply_cooldown(self, symbol: str):
        until = time.time() + self._cooldown_seconds
        self._stats[symbol].cooldown_until = until
        logger.warning(f"SymbolManager: {symbol} on cooldown for "
                       f"{self._cooldown_seconds//60} min after consecutive losses")

    def _in_cooldown(self, symbol: str) -> bool:
        return time.time() < self._stats[symbol].cooldown_until

    # ── Trade outcome recording ───────────────────────────────────────────────

    def record_trade(self, symbol: str, won: bool, pnl: float):
        if symbol not in self._stats:
            self._stats[symbol] = SymbolStats(symbol=symbol)
        st = self._stats[symbol]
        st.trades        += 1
        st.wins          += int(won)
        st.total_pnl     += pnl
        st.last_trade_ts  = time.time()

        # Track consecutive losses for cooldown
        if won:
            self._consecutive_losses[symbol] = 0
        else:
            n = self._consecutive_losses.get(symbol, 0) + 1
            self._consecutive_losses[symbol] = n
            if n >= self._cooldown_trades:
                self._apply_cooldown(symbol)
                self._consecutive_losses[symbol] = 0

    # ── Queue generation ──────────────────────────────────────────────────────

    def get_queue(self, max_symbols: int = 30) -> List[str]:
        """
        Return a prioritised list of symbols to scan next.
        Order: synthetic (always) → session-open + high-score → rest.
        """
        now = time.time()
        candidates = []

        for sym in sym_module.ALL_SYMBOLS:
            if sym not in self._active:
                continue
            if self._in_cooldown(sym):
                continue

            in_sess = self._in_session(sym)
            score   = self._stats[sym].score if sym in self._stats else 0.5
            candidates.append((sym, in_sess, score))

        # Sort: session-open first, then by score descending
        candidates.sort(key=lambda x: (not x[1], -x[2]))
        queue = [c[0] for c in candidates[:max_symbols]]

        # Always ensure at least some synthetics at the front
        synthetics_in_queue = [s for s in queue if s in sym_module.SYNTHETIC]
        if not synthetics_in_queue:
            queue = sym_module.SYNTHETIC[:3] + queue

        return queue

    def best_symbols(self, n: int = 5) -> List[dict]:
        """Return top N symbols by score for dashboard display."""
        scored = sorted(
            [(s, st) for s, st in self._stats.items() if st.trades > 0],
            key=lambda x: x[1].score,
            reverse=True
        )
        return [
            {"symbol": s, "trades": st.trades,
             "win_rate": round(st.win_rate * 100, 1),
             "pnl": round(st.total_pnl, 4),
             "score": round(st.score, 3)}
            for s, st in scored[:n]
        ]

    def all_stats(self) -> Dict[str, dict]:
        return {
            s: {"trades": st.trades, "wins": st.wins,
                "win_rate": round(st.win_rate * 100, 1),
                "pnl": round(st.total_pnl, 4),
                "cooldown": st.cooldown_until > time.time()}
            for s, st in self._stats.items()
            if st.trades > 0
        }

    @property
    def current_session(self) -> str:
        """Return active market session name(s) for display."""
        active = []
        now_utc = datetime.datetime.utcnow()
        h, m    = now_utc.hour, now_utc.minute
        now_min = h * 60 + m
        for name, (oh, om, ch, cm) in SESSIONS.items():
            open_min  = oh * 60 + om
            close_min = ch * 60 + cm
            if open_min < close_min:
                if open_min <= now_min <= close_min:
                    active.append(name.capitalize())
            else:
                if now_min >= open_min or now_min <= close_min:
                    active.append(name.capitalize())
        return " + ".join(active) if active else "Off-hours (synthetics only)"

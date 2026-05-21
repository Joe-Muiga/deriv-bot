"""
symbol_manager.py – Symbol rotation, suspension, session tracking, queue management.
v16 — TRADE_SYMBOLS only. No volatility indices. Full session-timing enforcement.

Suspension rules (unix-timestamp-based):
  WIN  → suspend 7 min  (420 s)
  LOSS → suspend 17 min (1020 s)
  3 losses same symbol same session → session ban (86400 s)

Same-symbol gap: 7 minutes minimum between placements.

Session timing (enforced in get_queue()):
  BOOM500           : 07:00–12:00 UTC
  CRASH500          : 07:00–16:00 UTC
  BOOM300/CRASH300  : 07:00–12:00 UTC
  BOOM1000/CRASH1000: 05:00–20:00 UTC
  All Boom/Crash    : dead zone 00:00–05:00 UTC → excluded
  Range Break       : always available (outside dead zone too)
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Dict, List, Optional, Set

import config

logger = logging.getLogger(__name__)

# ─── Symbol sets ──────────────────────────────────────────────────────────────
RANGE_BREAK_SYMBOLS : List[str] = list(config.RANGE_BREAK_SYMBOLS)
BOOM_CRASH_SYMBOLS  : List[str] = list(config.BOOM_CRASH_SYMBOLS)
TRADE_SYMBOLS       : List[str] = list(config.TRADE_SYMBOLS)

_RANGE_BREAK_SET : frozenset = frozenset(RANGE_BREAK_SYMBOLS)
_BOOM_CRASH_SET  : frozenset = frozenset(BOOM_CRASH_SYMBOLS)

# ─── Suspension constants (seconds) ──────────────────────────────────────────
WIN_SUSPEND_SECONDS  : int = config.SYMBOL_WIN_SUSPEND_MINS  * 60   # 420
LOSS_SUSPEND_SECONDS : int = config.SYMBOL_LOSS_SUSPEND_MINS * 60   # 1020
MIN_GAP_SECONDS      : int = config.SYMBOL_MIN_GAP_MINUTES   * 60   # 420
SESSION_BAN_LOSSES   : int = getattr(config, "SESSION_BAN_LOSS_THRESHOLD", 3)
SESSION_BAN_SECONDS  : int = 86400


# ─── Session stats ────────────────────────────────────────────────────────────

class _SessionStats:
    __slots__ = ("wins", "losses")

    def __init__(self) -> None:
        self.wins:   int = 0
        self.losses: int = 0

    @property
    def trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.5

    def reset(self) -> None:
        self.wins   = 0
        self.losses = 0


# ─── Session timing helpers ───────────────────────────────────────────────────

def _utc_hour() -> int:
    return datetime.datetime.utcnow().hour


def _in_window(hour: int, start: int, end: int) -> bool:
    """Return True if hour is within [start, end) UTC."""
    return start <= hour < end


def _symbol_session_allowed(symbol: str, hour: int) -> bool:
    """
    Return True if *symbol* is tradeable during *hour* (UTC).
    Range Break is always allowed.
    Boom/Crash: excluded 00:00–05:00 (dead zone) and symbol-specific windows.
    """
    if symbol in _RANGE_BREAK_SET:
        return True   # Range Break: no time restriction

    # All Boom/Crash: dead zone 00:00–05:00
    dead_start = getattr(config, "DEAD_ZONE_START_UTC", 0)
    dead_end   = getattr(config, "DEAD_ZONE_END_UTC",   5)
    if _in_window(hour, dead_start, dead_end):
        return False

    # Per-symbol windows
    if symbol == "BOOM500":
        return _in_window(hour, config.BOOM500_START_UTC, config.BOOM500_END_UTC)
    if symbol == "CRASH500":
        return _in_window(hour, config.CRASH500_START_UTC, config.CRASH500_END_UTC)
    if symbol in ("BOOM300", "CRASH300"):
        return _in_window(hour, config.BOOM_CRASH_300_START, config.BOOM_CRASH_300_END)
    if symbol in ("BOOM1000", "CRASH1000"):
        return _in_window(hour, config.BOOM_CRASH_1000_START, config.BOOM_CRASH_1000_END)

    return True   # any other Boom/Crash variant: allow outside dead zone


# ─── SymbolManager ────────────────────────────────────────────────────────────

class SymbolManager:
    """
    Central coordinator for symbol eligibility, unix-timestamp suspension,
    session P&L tracking, same-symbol gap enforcement, and session timing.

    All suspension logic uses unix timestamps (time.time()). No timedelta.
    Single-threaded asyncio; no locks needed.
    """

    def __init__(self) -> None:
        self._suspension_until : Dict[str, float] = {}
        self._last_traded      : Dict[str, float] = {}
        self._session_losses   : Dict[str, int]   = {}
        self._session          : Dict[str, _SessionStats] = {
            s: _SessionStats() for s in TRADE_SYMBOLS
        }
        self._cycle_used : Set[str]          = set()
        self._reset_task : Optional[asyncio.Task] = None

    # ─── Suspension ───────────────────────────────────────────────────────────

    def suspend(self, symbol: str, seconds: int) -> None:
        seconds = max(1, seconds)
        self._suspension_until[symbol] = time.time() + seconds
        logger.info(f"SUSPENDED: {symbol} for {seconds}s")

    def is_suspended(self, symbol: str) -> bool:
        return time.time() < self._suspension_until.get(symbol, 0.0)

    def suspension_remaining(self, symbol: str) -> float:
        rem = self._suspension_until.get(symbol, 0.0) - time.time()
        return max(0.0, rem)

    def lift_suspension(self, symbol: str) -> None:
        self._suspension_until.pop(symbol, None)

    # ─── Gap enforcement ──────────────────────────────────────────────────────

    def record_trade_placed(self, symbol: str) -> None:
        self._last_traded[symbol] = time.time()
        self._cycle_used.add(symbol)

    def can_trade_now(self, symbol: str) -> bool:
        """True if symbol is not suspended AND minimum gap has elapsed."""
        if self.is_suspended(symbol):
            return False
        last = self._last_traded.get(symbol, 0.0)
        return (time.time() - last) >= MIN_GAP_SECONDS

    def reset_cycle_used(self) -> None:
        self._cycle_used.clear()

    # ─── Result recording ─────────────────────────────────────────────────────

    def record_result(self, symbol: str, won: bool) -> None:
        stats = self._session.setdefault(symbol, _SessionStats())
        if won:
            stats.wins += 1
            self._session_losses[symbol] = 0
            self.suspend(symbol, WIN_SUSPEND_SECONDS)
            logger.info(f"RESULT WIN  {symbol} | wins={stats.wins} | suspended {WIN_SUSPEND_SECONDS}s")
        else:
            stats.losses += 1
            loss_count = self._session_losses.get(symbol, 0) + 1
            self._session_losses[symbol] = loss_count
            if loss_count >= SESSION_BAN_LOSSES:
                self.suspend(symbol, SESSION_BAN_SECONDS)
                logger.warning(f"SESSION BAN {symbol} | {loss_count} losses this session")
            else:
                self.suspend(symbol, LOSS_SUSPEND_SECONDS)
                logger.info(f"RESULT LOSS {symbol} | losses={stats.losses} | suspended {LOSS_SUSPEND_SECONDS}s")

    # ─── Queue generation ─────────────────────────────────────────────────────

    def get_queue(self) -> List[str]:
        """
        Returns tradeable symbols filtered by:
          1. Session timing (Boom/Crash UTC windows, dead zone)
          2. Not suspended
          3. Minimum gap since last trade
          4. Not already used this cycle

        Range Break always returned outside dead zone.
        """
        hour = _utc_hour()
        result: List[str] = []
        for sym in TRADE_SYMBOLS:
            if not _symbol_session_allowed(sym, hour):
                continue
            if self.is_suspended(sym):
                continue
            last = self._last_traded.get(sym, 0.0)
            if (time.time() - last) < MIN_GAP_SECONDS:
                continue
            if sym in self._cycle_used:
                continue
            result.append(sym)
        return result

    # ─── Session stats ────────────────────────────────────────────────────────

    def win_rate(self, symbol: str) -> float:
        return self._session.get(symbol, _SessionStats()).win_rate

    def session_stats(self, symbol: str) -> dict:
        s = self._session.get(symbol, _SessionStats())
        return {"wins": s.wins, "losses": s.losses, "win_rate": round(s.win_rate, 3)}

    def all_stats(self) -> dict:
        return {sym: self.session_stats(sym) for sym in TRADE_SYMBOLS}

    # ─── Session reset ────────────────────────────────────────────────────────

    def reset_session(self) -> None:
        for s in self._session.values():
            s.reset()
        self._session_losses.clear()
        self._suspension_until.clear()
        self._last_traded.clear()
        self._cycle_used.clear()
        logger.info("SymbolManager: session reset at UTC midnight")

    def start_midnight_reset_task(self) -> None:
        if self._reset_task is None or self._reset_task.done():
            self._reset_task = asyncio.create_task(self._midnight_reset_loop())

    async def _midnight_reset_loop(self) -> None:
        while True:
            now     = datetime.datetime.utcnow()
            midnight = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            wait = (midnight - now).total_seconds()
            await asyncio.sleep(max(wait, 1))
            self.reset_session()

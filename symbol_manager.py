"""
symbol_manager.py – Symbol rotation, suspension, session tracking, queue management.

Architectural principles (non-negotiable):
  1. Trade volume is a priority; capital protection via signal quality + sizing +
     suspension — never via reduced trade frequency.
  2. Parallelism is mandatory — asyncio.gather for every scan/execute/fetch.
     Sequential loops are a bug (caller responsibility for parallel execution).
  3. Signals drive entries; risk rules drive sizing — never the reverse.
  4. A 3/3 strength signal is unconditionally executable. No secondary gate,
     confidence check, or validation layer may block it.
  5. Failures are silent exits, never crashes. Every external call returns None
     on failure. Nothing raises unhandled exceptions.
  6. All artifacts are delivered complete and untruncated before any explanation.

Cycle lifecycle (caller must invoke in this exact order each cycle):
    queue = sm.get_queue()            # eligible symbols, filtered + sorted
    ... execute trades via asyncio.gather ...
    sm.record_trade_placed(symbol)    # record when trade was placed (gap enforcement)
    sm.record_result(symbol, won)     # atomically: update session stats + suspend

NOTE: decrement_all() is REMOVED — suspension is now time-based (datetime.utcnow()).
      Callers that previously called sm.decrement_all() should remove that call.

Session reset fires automatically at UTC midnight via start_midnight_reset_task().

Target instruments (priority order):
  Volatility:   R_10, R_25, R_50, R_75, R_100, 1HZ10V, 1HZ25V, 1HZ50V, 1HZ75V, 1HZ100V
  Boom/Crash:   BOOM500, CRASH500, BOOM1000, CRASH1000, BOOM300, CRASH300, BOOM150, CRASH150
  Range Break:  RDBULL, RDBEAR
  Step Index:   STPIDX (Step Index)

Suspension rules (time-based):
  WIN  → suspend 7 minutes
  LOSS → suspend 17 minutes
  3 losses same symbol same session → suspend 59940 minutes (rest of session)

Same-symbol gap enforcement:
  A symbol cannot be traded again within 7 minutes of its last trade placement.
  get_queue() silently filters symbols within their minimum gap.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Dict, List, Optional, Set, Tuple

import symbols as sym_module  # noqa: F401 — kept for backward compat with callers
import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Canonical symbol lists  (spec-defined, in priority order)
# ─────────────────────────────────────────────────────────────────────────────

VOLATILITY_SYMBOLS: List[str] = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
]

BOOM_CRASH_SYMBOLS: List[str] = [
    "BOOM500",  "CRASH500",
    "BOOM1000", "CRASH1000",
    "BOOM300",  "CRASH300",
    "BOOM150",  "CRASH150",
]

RANGE_BREAK_SYMBOLS: List[str] = ["RDBULL", "RDBEAR"]

STEP_SYMBOLS: List[str] = ["STPIDX"]

ALL_MANAGED_SYMBOLS: List[str] = (
    VOLATILITY_SYMBOLS + BOOM_CRASH_SYMBOLS + RANGE_BREAK_SYMBOLS + STEP_SYMBOLS
)

# ALL_SYMBOLS mirrors ALL_MANAGED_SYMBOLS; config reference name for get_queue()
ALL_SYMBOLS: List[str] = ALL_MANAGED_SYMBOLS

# Fast-lookup frozensets (constant after import)
_VOLATILITY_SET:   frozenset = frozenset(VOLATILITY_SYMBOLS)
_BOOM_CRASH_SET:   frozenset = frozenset(BOOM_CRASH_SYMBOLS)
_RANGE_BREAK_SET:  frozenset = frozenset(RANGE_BREAK_SYMBOLS)
_STEP_SET:         frozenset = frozenset(STEP_SYMBOLS)


# ─────────────────────────────────────────────────────────────────────────────
# Config constants  (read once at import time for fast per-cycle access)
# ─────────────────────────────────────────────────────────────────────────────

_DEAD_ZONE_START:    int = getattr(config, "DEAD_ZONE_START_UTC",        0)
_DEAD_ZONE_END:      int = getattr(config, "DEAD_ZONE_END_UTC",          5)
_SESSION_BAN_LOSSES: int = getattr(config, "SESSION_BAN_LOSS_THRESHOLD", 3)

# Time-based suspension constants (minutes)
_WIN_SUSPEND_MINUTES:        int = 7       # suspend after a win
_LOSS_SUSPEND_MINUTES:       int = 17      # suspend after a loss (< session ban threshold)
_SESSION_BAN_MINUTES:        int = 59_940  # 41.625 days ≈ rest of session until midnight reset
_MIN_TRADE_GAP_MINUTES:      int = 7       # minimum gap between trades on the same symbol


# ─────────────────────────────────────────────────────────────────────────────
# Per-symbol session statistics  (zeroed at UTC midnight, never replaced)
# ─────────────────────────────────────────────────────────────────────────────

class _SessionStats:
    """
    Lightweight win/loss accumulator for a single trading session.
    Instances persist across cycles and are reset (zeroed) by
    SymbolManager.reset_session() — they are never replaced or garbage-collected.
    """
    __slots__ = ("wins", "losses")

    def __init__(self) -> None:
        self.wins:   int = 0
        self.losses: int = 0

    @property
    def trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        """Neutral 0.5 when no trades have been recorded this session."""
        return self.wins / self.trades if self.trades > 0 else 0.5

    def reset(self) -> None:
        self.wins   = 0
        self.losses = 0


# ─────────────────────────────────────────────────────────────────────────────
# SymbolManager
# ─────────────────────────────────────────────────────────────────────────────

class SymbolManager:
    """
    Central coordinator for symbol eligibility, time-based suspension, session
    P&L tracking, same-symbol gap enforcement, dead-zone filtering, and trade
    queue generation.

    All public methods are synchronous and non-blocking.
    The midnight session-reset is driven by an asyncio background task.

    Suspension model: fully time-based (datetime.utcnow() comparisons).
      • suspend(symbol, minutes) sets an expiry timestamp.
      • is_suspended(symbol) compares utcnow() to that timestamp.
      • No per-cycle decrement required or performed.

    Same-symbol gap: last_traded[symbol] tracks when a trade was last placed.
      • can_trade_now(symbol) returns False within 7 minutes of last placement.
      • record_trade_placed(symbol) records the placement time.
      • get_queue() silently excludes symbols within their minimum gap.

    Concurrency model: single-threaded asyncio; no locks required.
    Parallelism for trade execution is the responsibility of the caller
    (bot_engine), which must use asyncio.gather for all concurrent operations.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Construction
    # ─────────────────────────────────────────────────────────────────────────

    def __init__(self) -> None:
        # Active set — starts with all managed symbols; narrowed via update_active()
        self._active: Set[str] = set(ALL_MANAGED_SYMBOLS)

        # Time-based suspension: maps symbol → expiry datetime (UTC).
        # Absent key ≡ not suspended ≡ expiry is datetime.min (always in the past).
        # Invariant: is_suspended(sym) == (utcnow() < suspend_until.get(sym, datetime.min))
        self._suspend_until: Dict[str, datetime.datetime] = {}

        # Same-symbol gap enforcement: maps symbol → datetime of last trade placement (UTC).
        # Absent key ≡ symbol has never been traded ≡ treated as datetime.min.
        self._last_traded: Dict[str, datetime.datetime] = {}

        # Session stats per symbol.  Pre-populated so get_symbol_score() never
        # needs to handle a missing key for a known symbol.
        self._session: Dict[str, _SessionStats] = {
            sym: _SessionStats() for sym in ALL_MANAGED_SYMBOLS
        }

        # Background midnight-reset task (None until start_midnight_reset_task())
        self._reset_task: Optional[asyncio.Task] = None

        # Per-cycle 'used' tracking (cleared by reset_cycle_used() each cycle)
        self._cycle_used: Set[str] = set()

    # ─────────────────────────────────────────────────────────────────────────
    # Suspension API  (time-based)
    # ─────────────────────────────────────────────────────────────────────────

    def suspend(self, symbol: str, minutes: int) -> None:
        """
        Suspend *symbol* for *minutes* minutes from now (UTC).

        Semantics
        ---------
        • The expiry is always computed as datetime.utcnow() + timedelta(minutes=minutes).
        • If the symbol is already suspended the expiry is REPLACED — fresh
          events restart the penalty at their full value, not stacked on top.
        • The symbol will NOT appear in get_queue() until the expiry passes.
        • minutes is clamped to a minimum of 1.
        """
        minutes = max(1, int(minutes))
        expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
        self._suspend_until[symbol] = expiry
        logger.info(
            f"SUSPEND: {symbol} → {minutes} minute(s) "
            f"(until {expiry.strftime('%H:%M:%S')} UTC)"
        )

    def is_suspended(self, symbol: str) -> bool:
        """
        Return True while the current UTC time is before the symbol's expiry.
        Returns False for unknown or expired symbols.
        """
        return datetime.datetime.utcnow() < self._suspend_until.get(
            symbol, datetime.datetime.min
        )

    def get_suspension_remaining(self, symbol: str) -> float:
        """
        Return the number of seconds *symbol* is still suspended for.
        Returns 0.0 when not suspended or expiry has passed.
        """
        expiry = self._suspend_until.get(symbol, datetime.datetime.min)
        remaining = (expiry - datetime.datetime.utcnow()).total_seconds()
        return max(0.0, remaining)

    def get_suspension_remaining_minutes(self, symbol: str) -> float:
        """
        Return the number of minutes *symbol* is still suspended for.
        Returns 0.0 when not suspended or expiry has passed.
        """
        return self.get_suspension_remaining(symbol) / 60.0

    # ─────────────────────────────────────────────────────────────────────────
    # Same-symbol gap enforcement
    # ─────────────────────────────────────────────────────────────────────────

    def record_trade_placed(self, symbol: str) -> None:
        """
        Record that a trade was just placed for *symbol*.
        Sets last_traded[symbol] = datetime.utcnow().

        Must be called by the caller immediately after a trade is placed, BEFORE
        the next get_queue() call, to ensure the gap enforcement is respected.
        """
        self._last_traded[symbol] = datetime.datetime.utcnow()
        logger.debug(f"TRADE PLACED: {symbol} last_traded updated to {self._last_traded[symbol].strftime('%H:%M:%S')} UTC")

    def can_trade_now(self, symbol: str) -> bool:
        """
        Return True if *symbol* is outside its minimum same-symbol trade gap.

        The minimum gap is _MIN_TRADE_GAP_MINUTES (7 minutes).
        Returns True for symbols that have never been traded (absent from last_traded).
        Returns False if the gap since last placement is < 7 minutes.
        """
        last = self._last_traded.get(symbol, datetime.datetime.min)
        elapsed = datetime.datetime.utcnow() - last
        return elapsed >= datetime.timedelta(minutes=_MIN_TRADE_GAP_MINUTES)

    def time_since_last_trade(self, symbol: str) -> float:
        """
        Return seconds elapsed since *symbol* was last traded.
        Returns infinity for symbols never traded this session.
        """
        last = self._last_traded.get(symbol)
        if last is None:
            return float("inf")
        return (datetime.datetime.utcnow() - last).total_seconds()

    def gap_remaining_seconds(self, symbol: str) -> float:
        """
        Return the number of seconds remaining in the minimum same-symbol gap.
        Returns 0.0 if the gap has elapsed or the symbol has never been traded.
        """
        last = self._last_traded.get(symbol, datetime.datetime.min)
        gap_end = last + datetime.timedelta(minutes=_MIN_TRADE_GAP_MINUTES)
        remaining = (gap_end - datetime.datetime.utcnow()).total_seconds()
        return max(0.0, remaining)

    # ─────────────────────────────────────────────────────────────────────────
    # Session result tracking  (reset at UTC midnight)
    # ─────────────────────────────────────────────────────────────────────────

    def record_result(self, symbol: str, won: bool) -> None:
        """
        Atomically record a trade result and apply the corresponding time-based
        suspension.

        WIN path
        --------
        • session_wins  += 1
        • suspend(symbol, 7)  — 7 minutes

        LOSS path
        ---------
        • session_losses += 1
        • If session_losses < SESSION_BAN_LOSS_THRESHOLD (default 3):
            suspend(symbol, 17)  — 17 minutes
        • If session_losses >= SESSION_BAN_LOSS_THRESHOLD:
            suspend(symbol, 59940)  — rest of session (≈ 41.6 days; expires at/past next midnight)

        Atomicity guarantee
        -------------------
        The session stats update and the suspension application occur in the
        same synchronous call body with no await points in between.
        No partial state is externally observable.
        """
        if symbol not in self._session:
            self._session[symbol] = _SessionStats()

        st = self._session[symbol]

        if won:
            st.wins += 1
            self.suspend(symbol, _WIN_SUSPEND_MINUTES)
            logger.info(
                f"RESULT WIN:  {symbol} | "
                f"Session {st.wins}W/{st.losses}L | "
                f"Suspended {_WIN_SUSPEND_MINUTES} minute(s)"
            )
        else:
            st.losses += 1
            if st.losses >= _SESSION_BAN_LOSSES:
                # Third (or subsequent) loss on same symbol this session → session ban
                self.suspend(symbol, _SESSION_BAN_MINUTES)
                logger.warning(
                    f"RESULT LOSS: {symbol} | "
                    f"Session {st.wins}W/{st.losses}L | "
                    f"*** SESSION BAN: {_SESSION_BAN_MINUTES} minutes "
                    f"(loss #{st.losses} this session) ***"
                )
            else:
                self.suspend(symbol, _LOSS_SUSPEND_MINUTES)
                logger.info(
                    f"RESULT LOSS: {symbol} | "
                    f"Session {st.wins}W/{st.losses}L | "
                    f"Suspended {_LOSS_SUSPEND_MINUTES} minute(s) "
                    f"({_SESSION_BAN_LOSSES - st.losses} loss(es) until session ban)"
                )

    def get_session_losses(self, symbol: str) -> int:
        """
        Return the number of losses recorded for *symbol* this session.
        Returns 0 for unknown or untouched symbols.
        """
        st = self._session.get(symbol)
        return st.losses if st else 0

    def reset_session(self) -> None:
        """
        Clear session win/loss counts for ALL symbols and lift all suspensions.

        Contract
        --------
        • Active suspension timestamps ARE cleared — a session-banned symbol
          re-enters the pool immediately after the midnight reset.
        • last_traded timestamps are cleared so gap enforcement resets cleanly.
        • Emits SESSION STATS log lines for every symbol with ≥ 1 trade
          BEFORE clearing, so the session's performance is preserved in logs.
        • Safe to call at any time; idempotent if no trades have occurred.
        """
        self._log_session_stats()
        for st in self._session.values():
            st.reset()
        # Clear time-based suspension state for the new session
        self._suspend_until.clear()
        # Clear last_traded so the new session starts without gap carry-over
        self._last_traded.clear()
        logger.info(
            "SESSION RESET: all session win/loss counts, suspension timestamps, "
            "and last_traded records cleared"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Symbol scoring
    # ─────────────────────────────────────────────────────────────────────────

    def get_symbol_score(self, symbol: str) -> float:
        """
        Return the session win rate for *symbol* as a float in [0.0, 1.0].

        Formula:  session_wins / session_trades
        Returns:  0.5  (neutral) when session_trades == 0
        """
        st = self._session.get(symbol)
        return st.win_rate if st else 0.5

    def best_symbols(self, n: int) -> List[str]:
        """
        Return the top *n* symbol names ordered by descending session win rate.

        Tie-breaking (equal win rate):
          1. More trades this session ranks higher (larger sample = more confidence).
          2. Symbols with 0 trades (score 0.5) rank below any symbol with ≥ 1 trade.

        Returns a List[str] of symbol names only (not dicts).
        """
        def _sort_key(sym: str) -> Tuple[float, int]:
            st = self._session.get(sym)
            if st is None or st.trades == 0:
                return (0.5, 0)
            return (st.win_rate, st.trades)

        ranked: List[str] = sorted(
            self._session.keys(),
            key=_sort_key,
            reverse=True,
        )
        return ranked[:n]

    # ─────────────────────────────────────────────────────────────────────────
    # Dead zone
    # ─────────────────────────────────────────────────────────────────────────

    def is_dead_zone(self) -> bool:
        """
        Return True if the current UTC hour falls within the dead zone:
            DEAD_ZONE_START_UTC <= hour < DEAD_ZONE_END_UTC

        Default range: [0, 5)  →  00:00 UTC (inclusive) to 05:00 UTC (exclusive).

        During the dead zone:
          • Boom/Crash symbols are excluded from get_queue().
          • Volatility indices are always included (dead zone has NO effect on them).
          • Range Break symbols remain eligible (secondary priority, no dead zone gate).
          • Step Index remains eligible (no dead zone gate).
        """
        hour: int = datetime.datetime.utcnow().hour
        return _DEAD_ZONE_START <= hour < _DEAD_ZONE_END

    # ─────────────────────────────────────────────────────────────────────────
    # Queue management
    # ─────────────────────────────────────────────────────────────────────────

    def get_queue(self, max_symbols: int = None) -> List[str]:
        """
        Return all eligible symbols from ALL_SYMBOLS (Boom/Crash + Volatility +
        Range Break + Step Index), filtered by suspension and same-symbol gap.

        Eligibility rules (applied unconditionally, in order):
          1. Symbol must be in the active set (_active).
          2. Symbol must NOT be suspended — is_suspended() evaluated here (time-based).
             A suspended symbol is NEVER returned under any circumstance.
          3. Symbol must pass same-symbol gap — can_trade_now() evaluated here.
             A symbol within its 7-minute minimum gap is NEVER returned.
          4. Dead zone filter — is_dead_zone() is called internally:
               • Boom/Crash symbols  → EXCLUDED during dead zone.
               • Volatility indices  → ALWAYS INCLUDED; dead zone has no effect.
               • Range Break symbols → INCLUDED during dead zone (secondary priority).
               • Step Index          → INCLUDED during dead zone (no gate).

        Priority ordering of the returned list:
          Priority 1 — Volatility indices   (R_*, 1HZ*V)        24/7, highest frequency
          Priority 2 — Range Break          (RDBULL, RDBEAR)     secondary
          Priority 3 — Step Index           (STPIDX)             tertiary
          Priority 4 — Boom/Crash           (BOOM*, CRASH*)      session-gated only

        Within each priority group, symbols are sorted by DESCENDING session win rate
        (get_symbol_score), so the historically better-performing symbol leads.

        Mandatory log line (emitted on every call):
            Queue: N symbols available | Suspended: [sym, ...] | In gap: [sym, ...]
        """
        dead_zone: bool = self.is_dead_zone()
        now = datetime.datetime.utcnow()

        # Build diagnostic lists for logging
        suspended_syms: List[str] = []
        in_gap_syms:    List[str] = []

        def _eligible(sym: str) -> bool:
            if sym not in self._active:
                return False
            if self.is_suspended(sym):
                suspended_syms.append(sym)
                return False
            if not self.can_trade_now(sym):
                in_gap_syms.append(sym)
                return False
            if dead_zone and sym in _BOOM_CRASH_SET:
                return False                        # Boom/Crash blocked during dead zone
            return True

        # Collect eligible symbols per priority group (preserving group order)
        vol_q:  List[str] = [s for s in VOLATILITY_SYMBOLS  if _eligible(s)]
        rb_q:   List[str] = [s for s in RANGE_BREAK_SYMBOLS if _eligible(s)]
        step_q: List[str] = [s for s in STEP_SYMBOLS        if _eligible(s)]
        bc_q:   List[str] = [s for s in BOOM_CRASH_SYMBOLS  if _eligible(s)]

        # Within each group: descending session win rate
        vol_q.sort( key=lambda s: -self.get_symbol_score(s))
        rb_q.sort(  key=lambda s: -self.get_symbol_score(s))
        step_q.sort(key=lambda s: -self.get_symbol_score(s))
        bc_q.sort(  key=lambda s: -self.get_symbol_score(s))

        result: List[str] = vol_q + rb_q + step_q + bc_q

        # Cap to max_symbols if provided
        if max_symbols is not None:
            result = result[:max_symbols]

        # Mandatory per-call log line
        logger.info(
            f"Queue: {len(result)} symbols available | "
            f"Suspended: {suspended_syms if suspended_syms else []} | "
            f"In gap: {in_gap_syms if in_gap_syms else []}"
        )

        return result

    def update_active(self, symbols: List[str]) -> None:
        """
        Refresh the active symbol set from an external source (e.g. Deriv API response).

        Volatility indices are unconditionally forced into the active set regardless
        of what the API returns — they are 24/7 instruments and must never be absent.

        Args:
            symbols: list of symbol names returned by the API active-symbols query.
        """
        self._active = set(symbols) | _VOLATILITY_SET
        logger.info(
            f"update_active: {len(self._active)} active symbols "
            f"({len(symbols)} from API + {len(_VOLATILITY_SET)} volatility forced)"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Session stats logging  (spec-defined format)
    # ─────────────────────────────────────────────────────────────────────────

    def _log_session_stats(self) -> None:
        """
        Emit one log line per symbol that has at least one trade this session.

        Format (spec-defined):
            SESSION STATS: {symbol} W:{wins} L:{losses} Rate:{rate:.1%}

        Lines are ordered by descending session win rate.
        Emitted automatically by reset_session(); also callable on demand via
        log_session_stats().
        """
        traded: List[Tuple[str, _SessionStats]] = [
            (sym, st)
            for sym, st in self._session.items()
            if st.trades > 0
        ]

        if not traded:
            logger.info("SESSION STATS: no trades recorded this session")
            return

        # Sort by descending win rate for human-readable output
        traded.sort(key=lambda x: -x[1].win_rate)

        for sym, st in traded:
            logger.info(
                f"SESSION STATS: {sym} "
                f"W:{st.wins} L:{st.losses} "
                f"Rate:{st.win_rate:.1%}"
            )

    def log_session_stats(self) -> None:
        """
        Public on-demand session stats dump.

        Emits one SESSION STATS log line for every symbol with ≥ 1 trade this
        session (spec-defined format).  Safe to call at any time; does not
        modify any state.
        """
        self._log_session_stats()

    # ─────────────────────────────────────────────────────────────────────────
    # Midnight reset background task
    # ─────────────────────────────────────────────────────────────────────────

    async def _midnight_reset_loop(self) -> None:
        """
        Asyncio coroutine: sleeps until the next UTC midnight, fires
        reset_session(), then loops indefinitely.

        Error handling (silent-exit principle):
          • asyncio.CancelledError is caught and exits the loop cleanly.
          • Any other exception is logged and the loop backs off 60 s before retry.
            The bot is never crashed by a reset failure.
        """
        while True:
            try:
                now_utc   = datetime.datetime.utcnow()
                tomorrow  = (now_utc + datetime.timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                sleep_sec = (tomorrow - now_utc).total_seconds()
                logger.info(
                    f"Midnight-reset: next session reset in {sleep_sec:.0f}s "
                    f"(at {tomorrow.isoformat()}Z)"
                )
                await asyncio.sleep(sleep_sec)
                logger.info("Midnight-reset: UTC midnight reached — resetting session")
                self.reset_session()

            except asyncio.CancelledError:
                logger.info("Midnight-reset task: cancelled cleanly")
                return                            # silent exit; do not re-raise

            except Exception as exc:              # never let this crash the bot
                logger.error(
                    f"Midnight-reset task non-fatal error: {exc!r} — "
                    f"retrying in 60s"
                )
                await asyncio.sleep(60)

    def start_midnight_reset_task(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> asyncio.Task:
        """
        Schedule the midnight session-reset coroutine on the event loop.

        Must be called after the asyncio event loop is running (e.g. inside an
        async main() or from bot_engine during startup).

        Args:
            loop: target event loop.  Defaults to the currently running loop
                  (asyncio.get_event_loop()).

        Returns:
            asyncio.Task — retain a reference and call stop_midnight_reset_task()
            (or task.cancel()) during graceful shutdown.
        """
        if loop is None:
            loop = asyncio.get_event_loop()
        self._reset_task = loop.create_task(
            self._midnight_reset_loop(),
            name="symbol_manager_midnight_reset",
        )
        logger.info("Midnight-reset task started (fires daily at UTC 00:00)")
        return self._reset_task

    def stop_midnight_reset_task(self) -> None:
        """
        Cancel the background midnight-reset task.
        Call this during graceful bot shutdown to avoid dangling tasks.
        No-op if the task is already done or was never started.
        """
        if self._reset_task and not self._reset_task.done():
            self._reset_task.cancel()
            logger.info("Midnight-reset task: stop requested")

    # ─────────────────────────────────────────────────────────────────────────
    # Introspection / dashboard helpers
    # ─────────────────────────────────────────────────────────────────────────

    def all_suspension_status(self) -> Dict[str, float]:
        """
        Return {symbol: minutes_remaining} for every currently suspended symbol.
        Empty dict when nothing is suspended.  Read-only snapshot; modifying
        the returned dict has no effect on internal state.
        """
        return {
            sym: self.get_suspension_remaining_minutes(sym)
            for sym, expiry in self._suspend_until.items()
            if datetime.datetime.utcnow() < expiry
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Compatibility shims  (called by bot_engine.py; aliases to canonical API)
    # ─────────────────────────────────────────────────────────────────────────

    def win_rate(self, symbol: str) -> float:
        """
        Alias for get_symbol_score().
        Returns the session win rate for *symbol* as a float in [0.0, 1.0].
        Neutral 0.5 when no session trades have been recorded.
        """
        return self.get_symbol_score(symbol)

    @property
    def current_session(self) -> str:
        """
        Return a human-readable session label based on the current UTC hour.
        Used by the dashboard; does not affect trading logic.
        """
        hour = datetime.datetime.utcnow().hour
        if 0 <= hour < 5:
            return "DEAD_ZONE"
        if 5 <= hour < 9:
            return "ASIA"
        if 9 <= hour < 13:
            return "LONDON"
        if 13 <= hour < 17:
            return "NEW_YORK"
        return "OVERLAP"

    def decrement_all(self) -> None:
        """
        NO-OP — retained for backward compatibility only.

        Suspension is now time-based (datetime.utcnow() comparisons).
        There are no per-cycle counters to decrement.
        Callers should remove calls to this method; it is harmless but unnecessary.
        """
        logger.debug(
            "decrement_all() called but is a no-op (suspension is now time-based)"
        )

    def decrement_suspensions(self) -> None:
        """
        NO-OP — retained for backward compatibility only.
        Alias for decrement_all(); see that method's docstring.
        """
        self.decrement_all()

    def reset_cycle_used(self) -> None:
        """
        Clear the per-cycle 'used' set so symbols can be traded again next cycle.
        Called by bot_engine at the start of each cycle.
        """
        self._cycle_used: Set[str] = set()

    def is_used(self, symbol: str) -> bool:
        """
        Return True if *symbol* has already been traded this cycle.
        Prevents the same symbol from being executed twice in one cycle.
        """
        return symbol in getattr(self, "_cycle_used", set())

    def mark_used(self, symbol: str) -> None:
        """
        Mark *symbol* as used for the current cycle.
        Subsequent is_used() calls will return True until reset_cycle_used().
        """
        if not hasattr(self, "_cycle_used"):
            self._cycle_used: Set[str] = set()
        self._cycle_used.add(symbol)

    def record_trade(self, symbol: str, won: bool, pnl: float = 0.0) -> None:
        """
        Legacy alias for record_result().
        Accepts an optional *pnl* kwarg (ignored; kept for call-site compatibility).
        """
        self.record_result(symbol=symbol, won=won)

    def all_session_stats(self) -> Dict[str, dict]:
        """
        Return a dict of session stats for all symbols with ≥ 1 trade this session.
        Suitable for API endpoints, dashboards, and test assertions.

        Schema per entry:
            {
                "wins":                int,
                "losses":              int,
                "trades":              int,
                "win_rate":            float  (4 d.p.),
                "suspended":           bool,
                "minutes_remaining":   float  (minutes left in suspension, 0.0 if none),
            }
        """
        return {
            sym: {
                "wins":              st.wins,
                "losses":            st.losses,
                "trades":            st.trades,
                "win_rate":          round(st.win_rate, 4),
                "suspended":         self.is_suspended(sym),
                "minutes_remaining": round(self.get_suspension_remaining_minutes(sym), 2),
            }
            for sym, st in self._session.items()
            if st.trades > 0
        }

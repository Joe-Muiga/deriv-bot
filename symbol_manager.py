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
    sm.decrement_all()               # once per cycle, BEFORE get_queue()
    queue = sm.get_queue()           # eligible symbols, filtered + sorted
    ... execute trades via asyncio.gather ...
    sm.record_result(symbol, won)    # atomically: update session stats + suspend

Session reset fires automatically at UTC midnight via start_midnight_reset_task().

Target instruments (priority order):
  Volatility:  R_10, R_25, R_50, R_75, R_100, 1HZ10V, 1HZ25V, 1HZ50V, 1HZ75V, 1HZ100V
  Boom/Crash:  BOOM500, CRASH500, BOOM1000, CRASH1000, BOOM300, CRASH300, BOOM150, CRASH150
  Range Break: RDBULL, RDBEAR
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

ALL_MANAGED_SYMBOLS: List[str] = (
    VOLATILITY_SYMBOLS + BOOM_CRASH_SYMBOLS + RANGE_BREAK_SYMBOLS
)

# Fast-lookup frozensets (constant after import)
_VOLATILITY_SET:  frozenset = frozenset(VOLATILITY_SYMBOLS)
_BOOM_CRASH_SET:  frozenset = frozenset(BOOM_CRASH_SYMBOLS)
_RANGE_BREAK_SET: frozenset = frozenset(RANGE_BREAK_SYMBOLS)


# ─────────────────────────────────────────────────────────────────────────────
# Config constants  (read once at import time for fast per-cycle access)
# ─────────────────────────────────────────────────────────────────────────────

_WIN_SUSPEND_CYCLES:  int = max(1, getattr(config, "SYMBOL_WIN_SUSPENSION_CYCLES",  2))
_LOSS_SUSPEND_CYCLES: int = max(1, getattr(config, "SYMBOL_LOSS_SUSPENSION_CYCLES", 3))
_DEAD_ZONE_START:     int = getattr(config, "DEAD_ZONE_START_UTC",            0)
_DEAD_ZONE_END:       int = getattr(config, "DEAD_ZONE_END_UTC",              5)
_SESSION_BAN_LOSSES:  int = getattr(config, "SESSION_BAN_LOSS_THRESHOLD",     3)
_SESSION_BAN_CYCLES:  int = 999  # effectively permanent until midnight reset


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
    Central coordinator for symbol eligibility, cycle-based suspension, session
    P&L tracking, dead-zone filtering, and trade queue generation.

    All public methods are synchronous and non-blocking.
    The midnight session-reset is driven by an asyncio background task.

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

        # Cycle-based suspension counters.
        # Invariant: an entry is present IFF cycles_remaining > 0.
        # Absent key  ≡  not suspended  ≡  get_suspension_remaining() == 0.
        self._suspend_cycles: Dict[str, int] = {}

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
    # Suspension API
    # ─────────────────────────────────────────────────────────────────────────

    def suspend(self, symbol: str, cycles: int) -> None:
        """
        Suspend *symbol* for *cycles* full trading cycles.

        Semantics
        ---------
        • *cycles* is clamped to a minimum of 1.
        • If the symbol is already suspended the counter is REPLACED — fresh
          events restart the penalty at their full value, not stacked on top.
        • The symbol will NOT appear in get_queue() until the counter reaches 0.
        """
        cycles = max(1, int(cycles))
        self._suspend_cycles[symbol] = cycles
        logger.info(f"SUSPEND: {symbol} → {cycles} cycle(s)")

    def is_suspended(self, symbol: str) -> bool:
        """Return True while cycles_remaining > 0."""
        return self._suspend_cycles.get(symbol, 0) > 0

    def get_suspension_remaining(self, symbol: str) -> int:
        """
        Return the number of cycles *symbol* is still suspended for.
        Returns 0 when not suspended (key absent from counter dict).
        """
        return self._suspend_cycles.get(symbol, 0)

    def decrement_all(self) -> None:
        """
        Decrement every active suspension counter by 1.
        Counters that reach ≤ 0 are removed; those symbols re-enter the pool
        immediately (i.e. they will appear in the very next get_queue() call).

        Contract
        --------
        • MUST be called exactly ONCE per cycle, BEFORE get_queue().
        • Emits the mandatory per-cycle log line (post-decrement snapshot):

            SUSPENDED: [SYM(n), ...] | ACTIVE: N symbols | DEAD_ZONE: YES/NO

          where n is the remaining cycles AFTER decrement.
        """
        # ── Decrement and prune ───────────────────────────────────────────────
        expired: List[str] = []
        for sym in list(self._suspend_cycles):
            new_val = self._suspend_cycles[sym] - 1
            if new_val <= 0:
                expired.append(sym)
                del self._suspend_cycles[sym]
            else:
                self._suspend_cycles[sym] = new_val

        for sym in expired:
            logger.info(f"SUSPENSION EXPIRED: {sym} re-entering pool")

        # ── Post-decrement snapshot for the mandatory log line ────────────────
        dead_zone = self.is_dead_zone()

        # Build SUSPENDED segment: SYM(remaining) for each still-suspended symbol
        suspended_parts: List[str] = [
            f"{sym}({rem})"
            for sym, rem in sorted(self._suspend_cycles.items())
            if rem > 0
        ]
        sus_str: str = ", ".join(suspended_parts) if suspended_parts else "none"

        # ACTIVE count: symbols in _active that pass both the suspension and
        # dead-zone filters (mirrors get_queue() eligibility exactly)
        active_count: int = sum(
            1
            for sym in self._active
            if not self.is_suspended(sym)
            and not (dead_zone and sym in _BOOM_CRASH_SET)
        )

        # Mandatory per-cycle log (spec-defined format)
        logger.info(
            f"SUSPENDED: [{sus_str}] | "
            f"ACTIVE: {active_count} symbols | "
            f"DEAD_ZONE: {'YES' if dead_zone else 'NO'}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Session result tracking  (reset at UTC midnight)
    # ─────────────────────────────────────────────────────────────────────────

    def record_result(self, symbol: str, won: bool) -> None:
        """
        Atomically record a trade result and apply the corresponding suspension.

        WIN path
        --------
        • session_wins  += 1
        • suspend for SYMBOL_WIN_SUSPENSION_CYCLES cycles

        LOSS path
        ---------
        • session_losses += 1
        • If session_losses < SESSION_BAN_LOSS_THRESHOLD (default 3):
            suspend for SYMBOL_LOSS_SUSPENSION_CYCLES cycles
        • If session_losses >= SESSION_BAN_LOSS_THRESHOLD:
            suspend for 999 cycles (session ban — symbol gone until midnight reset)

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
            win_cycles = getattr(
                config, "SYMBOL_WIN_SUSPENSION_CYCLES", _WIN_SUSPEND_CYCLES
            )
            self.suspend(symbol, win_cycles)
            logger.info(
                f"RESULT WIN:  {symbol} | "
                f"Session {st.wins}W/{st.losses}L | "
                f"Suspended {win_cycles} cycle(s)"
            )
        else:
            st.losses += 1
            if st.losses >= _SESSION_BAN_LOSSES:
                # Third (or subsequent) loss on same symbol this session → ban
                self.suspend(symbol, _SESSION_BAN_CYCLES)
                logger.warning(
                    f"RESULT LOSS: {symbol} | "
                    f"Session {st.wins}W/{st.losses}L | "
                    f"*** SESSION BAN: {_SESSION_BAN_CYCLES} cycles "
                    f"(loss #{st.losses} this session) ***"
                )
            else:
                loss_cycles = getattr(
                    config, "SYMBOL_LOSS_SUSPENSION_CYCLES", _LOSS_SUSPEND_CYCLES
                )
                self.suspend(symbol, loss_cycles)
                logger.info(
                    f"RESULT LOSS: {symbol} | "
                    f"Session {st.wins}W/{st.losses}L | "
                    f"Suspended {loss_cycles} cycle(s) "
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
        Clear session win/loss counts for ALL symbols.

        Contract
        --------
        • Active suspension counters are NOT touched — a session-banned symbol
          (999 cycles) remains suspended after the reset.
        • Emits SESSION STATS log lines for every symbol with ≥ 1 trade
          BEFORE clearing, so the session's performance is preserved in logs.
        • Safe to call at any time; idempotent if no trades have occurred.
        """
        self._log_session_stats()
        for st in self._session.values():
            st.reset()
        logger.info(
            "SESSION RESET: all session win/loss counts cleared "
            "(active suspensions preserved)"
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
        """
        hour: int = datetime.datetime.utcnow().hour
        return _DEAD_ZONE_START <= hour < _DEAD_ZONE_END

    # ─────────────────────────────────────────────────────────────────────────
    # Queue management
    # ─────────────────────────────────────────────────────────────────────────

    def get_queue(self, max_symbols: int = None) -> List[str]:
        """
        Return all active, non-suspended symbols.
        If max_symbols is provided, cap the returned list at that count.
        Dead zone filtering applied internally.
        Volatility indices always included regardless of dead zone.
        Boom/Crash excluded during dead zone hours (00:00–05:00 UTC).

        Eligibility rules (applied unconditionally, in order):
          1. Symbol must be in the active set (_active).
          2. Symbol must NOT be suspended — is_suspended() is evaluated here.
             A suspended symbol is NEVER returned under any circumstance.
          3. Dead zone filter — is_dead_zone() is called internally:
               • Boom/Crash symbols  → EXCLUDED during dead zone.
               • Volatility indices  → ALWAYS INCLUDED; dead zone has no effect.
               • Range Break symbols → INCLUDED during dead zone (secondary priority).

        Priority ordering of the returned list:
          Priority 1 — Volatility indices   (R_*, 1HZ*V)        24/7, highest frequency
          Priority 2 — Range Break          (RDBULL, RDBEAR)     secondary
          Priority 3 — Boom/Crash           (BOOM*, CRASH*)      session-gated only

        Within each priority group, symbols are sorted by DESCENDING session win rate
        (get_symbol_score), so the historically better-performing symbol leads.

        Calling decrement_all() before this method each cycle is required for the
        suspension counters to be current.
        """
        dead_zone: bool = self.is_dead_zone()

        def _eligible(sym: str) -> bool:
            if sym not in self._active:
                return False
            if self.is_suspended(sym):              # UNCONDITIONAL: never return suspended
                return False
            if dead_zone and sym in _BOOM_CRASH_SET:
                return False                        # Boom/Crash blocked during dead zone
            return True

        # Collect eligible symbols per priority group (preserving group order)
        vol_q: List[str] = [s for s in VOLATILITY_SYMBOLS  if _eligible(s)]
        rb_q:  List[str] = [s for s in RANGE_BREAK_SYMBOLS if _eligible(s)]
        bc_q:  List[str] = [s for s in BOOM_CRASH_SYMBOLS  if _eligible(s)]

        # Within each group: descending session win rate
        vol_q.sort(key=lambda s: -self.get_symbol_score(s))
        rb_q.sort( key=lambda s: -self.get_symbol_score(s))
        bc_q.sort( key=lambda s: -self.get_symbol_score(s))

        result: List[str] = vol_q + rb_q + bc_q

        # Cap to max_symbols if provided
        if max_symbols is not None:
            result = result[:max_symbols]

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

    def all_suspension_status(self) -> Dict[str, int]:
        """
        Return {symbol: cycles_remaining} for every currently suspended symbol.
        Empty dict when nothing is suspended.  Read-only snapshot; modifying
        the returned dict has no effect on internal state.
        """
        return {sym: rem for sym, rem in self._suspend_cycles.items() if rem > 0}

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

    def decrement_suspensions(self) -> None:
        """
        Alias for decrement_all().
        Decrement every active suspension counter by 1.
        Must be called exactly once per cycle, BEFORE get_queue().
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
                "wins":             int,
                "losses":           int,
                "trades":           int,
                "win_rate":         float  (4 d.p.),
                "suspended":        bool,
                "cycles_remaining": int,
            }
        """
        return {
            sym: {
                "wins":             st.wins,
                "losses":           st.losses,
                "trades":           st.trades,
                "win_rate":         round(st.win_rate, 4),
                "suspended":        self.is_suspended(sym),
                "cycles_remaining": self.get_suspension_remaining(sym),
            }
            for sym, st in self._session.items()
            if st.trades > 0
        }

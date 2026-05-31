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

All timing is unix-timestamp-based (time.time()). No cycle counters. No datetime
arithmetic for suspension math. No per-cycle decrement calls.

Session reset fires automatically at UTC midnight via start_midnight_reset_task().

Target instruments (priority order):
  Volatility:   R_10, R_25, R_50, R_75, R_100, 1HZ10V, 1HZ25V, 1HZ50V, 1HZ75V, 1HZ100V
  Boom/Crash:   BOOM500, CRASH500, BOOM1000, CRASH1000, BOOM300, CRASH300, BOOM150, CRASH150
  Range Break:  RDBULL, RDBEAR
  Step Index:   STPIDX
  Jump:         JUMP10, JUMP25, JUMP50, JUMP75, JUMP100
  Digit/Mean:   configured via config.DIGIT_SYMBOLS + config.MEAN_REVERSION (if present)

Suspension rules (unix-timestamp-based):
  WIN  → suspend config.SYMBOL_WIN_SUSPEND_MINS minutes
  LOSS → suspend config.SYMBOL_LOSS_SUSPEND_MINS minutes
  config.SYMBOL_SESSION_BAN_LOSSES consecutive losses same symbol → suspend 59940s (~rest of session)

Same-symbol gap enforcement:
  A symbol cannot be traded again within config.SYMBOL_MIN_GAP_MINS minutes of its
  last trade placement. get_queue() silently filters symbols within their minimum gap.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Dict, List, Optional, Set, Tuple

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

RANGE_BREAK_SYMBOLS: List[str] = getattr(config, "RANGE_BREAK_SYMBOLS", ["RDBULL", "RDBEAR"])

STEP_SYMBOLS: List[str] = getattr(config, "STEP_SYMBOLS", ["STPIDX"])

JUMP_SYMBOLS: List[str] = getattr(config, "JUMP_SYMBOLS", [
    "JUMP10", "JUMP25", "JUMP50", "JUMP75", "JUMP100",
])

DIGIT_SYMBOLS: List[str] = getattr(config, "DIGIT_SYMBOLS", [])

MEAN_REVERSION: List[str] = getattr(config, "MEAN_REVERSION", [])

ALL_MANAGED_SYMBOLS: List[str] = (
    VOLATILITY_SYMBOLS
    + BOOM_CRASH_SYMBOLS
    + RANGE_BREAK_SYMBOLS
    + STEP_SYMBOLS
    + JUMP_SYMBOLS
    + DIGIT_SYMBOLS
    + MEAN_REVERSION
)

# ALL_SYMBOLS mirrors ALL_MANAGED_SYMBOLS for config reference
ALL_SYMBOLS: List[str] = ALL_MANAGED_SYMBOLS

# Fast-lookup frozensets (constant after import)
_VOLATILITY_SET:    frozenset = frozenset(VOLATILITY_SYMBOLS)
_BOOM_CRASH_SET:    frozenset = frozenset(BOOM_CRASH_SYMBOLS)
_RANGE_BREAK_SET:   frozenset = frozenset(RANGE_BREAK_SYMBOLS)
_STEP_SET:          frozenset = frozenset(STEP_SYMBOLS)
_JUMP_SET:          frozenset = frozenset(JUMP_SYMBOLS)
_DIGIT_SET:         frozenset = frozenset(DIGIT_SYMBOLS)
_MEAN_REVERSION_SET:frozenset = frozenset(MEAN_REVERSION)
_247_SET:           frozenset = (
    _VOLATILITY_SET | _RANGE_BREAK_SET | _STEP_SET | _DIGIT_SET | _MEAN_REVERSION_SET
)

# Specific boom/crash session windows
_BOOM500_SET:   frozenset = frozenset(["BOOM500"])
_BOOM300_SET:   frozenset = frozenset(["BOOM300", "BOOM300N"])
_CRASH300_SET:  frozenset = frozenset(["CRASH300", "CRASH300N"])
_CRASH500_SET:  frozenset = frozenset(["CRASH500"])
_BOOM1000_SET:  frozenset = frozenset(["BOOM1000"])
_CRASH1000_SET: frozenset = frozenset(["CRASH1000"])


# ─────────────────────────────────────────────────────────────────────────────
# Config constants  (read at call time via getattr for live-reload support)
# ─────────────────────────────────────────────────────────────────────────────

def _win_suspend_secs() -> float:
    return getattr(config, "SYMBOL_WIN_SUSPEND_MINS", 7) * 60

def _loss_suspend_secs() -> float:
    return getattr(config, "SYMBOL_LOSS_SUSPEND_MINS", 17) * 60

def _min_gap_secs() -> float:
    return getattr(config, "SYMBOL_MIN_GAP_MINS", 7) * 60

def _session_ban_losses() -> int:
    return getattr(config, "SYMBOL_SESSION_BAN_LOSSES", 3)

SESSION_BAN_SECONDS: int = 59940   # ~16.65 hours — covers rest of session until midnight


# ─────────────────────────────────────────────────────────────────────────────
# Module-level state  (unix-timestamp-based, no cycle counters)
# ─────────────────────────────────────────────────────────────────────────────

_suspension_until: Dict[str, float] = {}   # symbol -> unix expiry timestamp
_last_traded:      Dict[str, float] = {}   # symbol -> unix timestamp of last placement
_session_losses:   Dict[str, int]   = {}   # symbol -> loss count this UTC session
_symbol_wins:      Dict[str, int]   = {}   # symbol -> win count this session
_symbol_trades:    Dict[str, int]   = {}   # symbol -> total trade count this session
_active_symbols:   Set[str]         = set()  # symbols with currently open contracts
_jump_last_seen:   Dict[str, float] = {}   # symbol -> unix timestamp of last detected jump


# ─────────────────────────────────────────────────────────────────────────────
# Suspension
# ─────────────────────────────────────────────────────────────────────────────

def suspend(symbol: str, minutes: float) -> None:
    """
    Suspend *symbol* for *minutes* minutes from now.
    Sets _suspension_until[symbol] = time.time() + minutes*60.
    Replaces any existing suspension.
    """
    seconds = max(1.0, float(minutes) * 60)
    expiry = time.time() + seconds
    _suspension_until[symbol] = expiry
    logger.info(
        f"SUSPENDED: {symbol} for {minutes:.1f}m "
        f"(until unix={expiry:.0f}, ~{int(seconds)}s from now)"
    )


def is_suspended(symbol: str) -> bool:
    """
    Return True while time.time() < _suspension_until[symbol].
    Logs remaining seconds if suspended.
    """
    until = _suspension_until.get(symbol, 0.0)
    now = time.time()
    if now < until:
        remaining = until - now
        logger.debug(
            f"BLOCKED: {symbol} suspended — {remaining:.0f}s remaining"
        )
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Trade eligibility
# ─────────────────────────────────────────────────────────────────────────────

def can_trade_now(symbol: str) -> bool:
    """
    Return False if:
      - is_suspended(symbol) is True
      - symbol in _active_symbols (open contract already exists)
      - time since last placement < config.SYMBOL_MIN_GAP_MINS * 60
    Logs exact reason for False.
    """
    if is_suspended(symbol):
        logger.info(f"CAN_TRADE_NOW False: {symbol} — currently suspended")
        return False

    if symbol in _active_symbols:
        logger.info(f"CAN_TRADE_NOW False: {symbol} — open contract active")
        return False

    elapsed = time.time() - _last_traded.get(symbol, 0.0)
    gap = _min_gap_secs()
    if elapsed < gap:
        remaining = gap - elapsed
        logger.info(
            f"CAN_TRADE_NOW False: {symbol} — min gap not elapsed "
            f"({remaining:.0f}s remaining)"
        )
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Trade lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def record_trade_placed(symbol: str) -> None:
    """
    Record that a trade was placed. Sets _last_traded[symbol] = time.time()
    and adds symbol to _active_symbols.
    """
    _last_traded[symbol] = time.time()
    _active_symbols.add(symbol)
    logger.info(
        f"TRADE PLACED: {symbol} at unix={_last_traded[symbol]:.0f}"
    )


def record_contract_opened(symbol: str) -> None:
    """Add *symbol* to _active_symbols (contract confirmed open)."""
    _active_symbols.add(symbol)
    logger.debug(f"CONTRACT OPENED: {symbol} added to active set")


def record_contract_closed(symbol: str) -> None:
    """Remove *symbol* from _active_symbols (contract settled)."""
    _active_symbols.discard(symbol)
    logger.debug(f"CONTRACT CLOSED: {symbol} removed from active set")


# ─────────────────────────────────────────────────────────────────────────────
# Result recording
# ─────────────────────────────────────────────────────────────────────────────

def record_result(symbol: str, won: bool) -> None:
    """
    Record a trade result and apply the corresponding unix-timestamp suspension.

    WIN  → suspend(symbol, config.SYMBOL_WIN_SUSPEND_MINS); increment _symbol_wins
    LOSS → increment _session_losses[symbol]
           If >= config.SYMBOL_SESSION_BAN_LOSSES: suspend 59940s and log session ban
           Else: suspend(symbol, config.SYMBOL_LOSS_SUSPEND_MINS)
    Always: increment _symbol_trades[symbol]
    """
    _symbol_trades[symbol] = _symbol_trades.get(symbol, 0) + 1

    if won:
        _symbol_wins[symbol] = _symbol_wins.get(symbol, 0) + 1
        win_mins = getattr(config, "SYMBOL_WIN_SUSPEND_MINS", 7)
        suspend(symbol, win_mins)
        logger.info(
            f"RESULT WIN:  {symbol} | "
            f"Session {_symbol_wins.get(symbol,0)}W/"
            f"{_session_losses.get(symbol,0)}L | "
            f"Suspended {win_mins}m"
        )
    else:
        _session_losses[symbol] = _session_losses.get(symbol, 0) + 1
        loss_count = _session_losses[symbol]
        ban_threshold = _session_ban_losses()

        if loss_count >= ban_threshold:
            _suspension_until[symbol] = time.time() + SESSION_BAN_SECONDS
            logger.warning(
                f"RESULT LOSS: {symbol} | "
                f"Session {_symbol_wins.get(symbol,0)}W/{loss_count}L | "
                f"*** SESSION BAN: {SESSION_BAN_SECONDS}s "
                f"(loss #{loss_count} — threshold {ban_threshold}) ***"
            )
        else:
            loss_mins = getattr(config, "SYMBOL_LOSS_SUSPEND_MINS", 17)
            suspend(symbol, loss_mins)
            remaining_until_ban = ban_threshold - loss_count
            logger.info(
                f"RESULT LOSS: {symbol} | "
                f"Session {_symbol_wins.get(symbol,0)}W/{loss_count}L | "
                f"Suspended {loss_mins}m "
                f"({remaining_until_ban} loss(es) until session ban)"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Symbol scoring
# ─────────────────────────────────────────────────────────────────────────────

def get_symbol_score(symbol: str) -> float:
    """
    Return session win rate for *symbol* as float in [0.0, 1.0].
    Returns 0.5 (neutral) when no trades recorded.
    """
    wins   = _symbol_wins.get(symbol, 0)
    trades = _symbol_trades.get(symbol, 0)
    return wins / max(trades, 1) if trades > 0 else 0.5


def best_symbols(n: int) -> List[dict]:
    """
    Return top *n* symbols by session win rate as a list of dicts:
        [{"symbol": str, "win_rate": float, "trades": int}, ...]

    Tie-breaking: more trades ranks higher (larger sample = more confidence).
    Symbols with 0 trades (score 0.5) rank below any symbol with >= 1 trade.
    """
    all_syms = set(_symbol_trades.keys()) | set(_symbol_wins.keys())

    def _sort_key(sym: str) -> Tuple[float, int]:
        trades = _symbol_trades.get(sym, 0)
        if trades == 0:
            return (0.5, 0)
        wins = _symbol_wins.get(sym, 0)
        return (wins / trades, trades)

    ranked = sorted(all_syms, key=_sort_key, reverse=True)[:n]
    return [
        {
            "symbol":   sym,
            "win_rate": round(get_symbol_score(sym), 4),
            "trades":   _symbol_trades.get(sym, 0),
        }
        for sym in ranked
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Session window filter
# ─────────────────────────────────────────────────────────────────────────────

def is_in_session(symbol: str) -> bool:
    """
    Check current UTC hour against configured session windows.

    Session rules:
      BOOM500 / BOOM300 / CRASH300  : 07:00–12:00 UTC only
      CRASH500                      : 07:00–16:00 UTC only
      BOOM1000 / CRASH1000          : 05:00–20:00 UTC only
      RANGE_BREAK_SYMBOLS           : always True (24/7)
      DIGIT_SYMBOLS + MEAN_REVERSION: always True (24/7)
      STEP_SYMBOLS                  : always True (24/7)
      JUMP_SYMBOLS                  : prefer 07:00–20:00 UTC but allow anytime
      VOLATILITY_SYMBOLS            : always True (24/7)
      Dead zone 00:00–05:00 UTC     : return False for all Boom/Crash
    """
    hour: int = datetime.datetime.utcnow().hour
    dead_zone: bool = 0 <= hour < 5

    # Dead zone blocks all Boom/Crash unconditionally
    if dead_zone and symbol in _BOOM_CRASH_SET:
        return False

    # 24/7 instruments — no session gate
    if symbol in _247_SET:
        return True

    # VOLATILITY (already in _247_SET but explicit for clarity)
    if symbol in _VOLATILITY_SET:
        return True

    # RANGE_BREAK — 24/7
    if symbol in _RANGE_BREAK_SET:
        return True

    # STEP — 24/7
    if symbol in _STEP_SET:
        return True

    # JUMP — preferred 07:00–20:00, allowed anytime
    if symbol in _JUMP_SET:
        preferred = 7 <= hour < 20
        if not preferred:
            logger.debug(
                f"IS_IN_SESSION: {symbol} outside preferred window "
                f"(hour={hour} UTC) — allowing anyway"
            )
        return True  # always allowed

    # BOOM500 / BOOM300 / CRASH300: 07:00–12:00 UTC
    if symbol in (_BOOM500_SET | _BOOM300_SET | _CRASH300_SET):
        return 7 <= hour < 12

    # CRASH500: 07:00–16:00 UTC
    if symbol in _CRASH500_SET:
        return 7 <= hour < 16

    # BOOM1000 / CRASH1000: 05:00–20:00 UTC
    if symbol in (_BOOM1000_SET | _CRASH1000_SET):
        return 5 <= hour < 20

    # Any remaining BOOM/CRASH variant (BOOM150, CRASH150, etc.): 07:00–16:00 UTC
    if symbol in _BOOM_CRASH_SET:
        return 7 <= hour < 16

    # Unknown symbol: allow (fail-open, caller decides)
    logger.debug(f"IS_IN_SESSION: {symbol} unknown — defaulting to True")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Queue generation
# ─────────────────────────────────────────────────────────────────────────────

def get_queue(active_list: List[str]) -> List[str]:
    """
    From *active_list*, return symbols where:
      - can_trade_now(symbol) is True
      - is_in_session(symbol) is True
      - symbol is in config.TRADE_SYMBOLS

    Result is ordered by descending session win rate within each priority group:
      Priority 1 — Volatility (R_*, 1HZ*V)
      Priority 2 — Range Break (RDBULL, RDBEAR)
      Priority 3 — Step Index
      Priority 4 — Jump
      Priority 5 — Digit / Mean Reversion
      Priority 6 — Boom/Crash (session-gated)

    Mandatory log line (emitted on every call):
        Queue: {N} tradeable | Suspended: [list] | Session-blocked: [list]
    """
    trade_symbols: Set[str] = set(getattr(config, "TRADE_SYMBOLS", ALL_MANAGED_SYMBOLS))

    suspended_syms:       List[str] = []
    session_blocked_syms: List[str] = []

    eligible: List[str] = []

    for sym in active_list:
        if sym not in trade_symbols:
            continue

        if not can_trade_now(sym):
            # Distinguish suspension from gap/open-contract for logging
            if is_suspended(sym) or sym in _active_symbols:
                if sym not in suspended_syms:
                    suspended_syms.append(sym)
            # gap-blocked shares suspended bucket for simplicity in log line
            else:
                if sym not in suspended_syms:
                    suspended_syms.append(sym)
            continue

        if not is_in_session(sym):
            if sym not in session_blocked_syms:
                session_blocked_syms.append(sym)
            continue

        eligible.append(sym)

    # Priority group buckets
    vol_q:    List[str] = [s for s in eligible if s in _VOLATILITY_SET]
    rb_q:     List[str] = [s for s in eligible if s in _RANGE_BREAK_SET]
    step_q:   List[str] = [s for s in eligible if s in _STEP_SET]
    jump_q:   List[str] = [s for s in eligible if s in _JUMP_SET]
    digit_q:  List[str] = [s for s in eligible if s in (_DIGIT_SET | _MEAN_REVERSION_SET)]
    bc_q:     List[str] = [
        s for s in eligible
        if s in _BOOM_CRASH_SET
        and s not in _VOLATILITY_SET  # guard against overlap
    ]

    # Sort each group by descending session win rate
    for group in (vol_q, rb_q, step_q, jump_q, digit_q, bc_q):
        group.sort(key=lambda s: -get_symbol_score(s))

    result: List[str] = vol_q + rb_q + step_q + jump_q + digit_q + bc_q

    logger.info(
        f"Queue: {len(result)} tradeable | "
        f"Suspended: {suspended_syms if suspended_syms else []} | "
        f"Session-blocked: {session_blocked_syms if session_blocked_syms else []}"
    )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Session reset
# ─────────────────────────────────────────────────────────────────────────────

def reset_session() -> None:
    """
    Clear _session_losses, _symbol_wins, _symbol_trades at UTC midnight.
    Suspensions (_suspension_until) and _last_traded are preserved.
    Emits session stats before clearing.
    """
    # Log stats before clearing
    traded_syms = [s for s in _symbol_trades if _symbol_trades[s] > 0]
    if traded_syms:
        traded_syms.sort(key=lambda s: -get_symbol_score(s))
        for sym in traded_syms:
            wins   = _symbol_wins.get(sym, 0)
            losses = _session_losses.get(sym, 0)
            trades = _symbol_trades.get(sym, 0)
            rate   = get_symbol_score(sym)
            logger.info(
                f"SESSION STATS: {sym} "
                f"W:{wins} L:{losses} T:{trades} Rate:{rate:.1%}"
            )
    else:
        logger.info("SESSION STATS: no trades recorded this session")

    _session_losses.clear()
    _symbol_wins.clear()
    _symbol_trades.clear()

    logger.info(
        "SESSION RESET: _session_losses, _symbol_wins, _symbol_trades cleared. "
        "Suspensions and last_traded timestamps preserved."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Utility / introspection helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_suspension_remaining(symbol: str) -> float:
    """Return seconds remaining in suspension for *symbol*. 0.0 if not suspended."""
    return max(0.0, _suspension_until.get(symbol, 0.0) - time.time())


def get_suspension_remaining_minutes(symbol: str) -> float:
    """Return minutes remaining in suspension for *symbol*. 0.0 if not suspended."""
    return get_suspension_remaining(symbol) / 60.0


def time_since_last_trade(symbol: str) -> float:
    """Return seconds elapsed since *symbol* was last traded. inf if never."""
    last = _last_traded.get(symbol)
    return float("inf") if last is None else time.time() - last


def gap_remaining_seconds(symbol: str) -> float:
    """Return seconds remaining in minimum same-symbol gap. 0.0 if gap elapsed."""
    last = _last_traded.get(symbol, 0.0)
    return max(0.0, last + _min_gap_secs() - time.time())


def all_suspension_status() -> Dict[str, float]:
    """Return {symbol: minutes_remaining} for every currently suspended symbol."""
    now = time.time()
    return {
        sym: (until - now) / 60.0
        for sym, until in _suspension_until.items()
        if now < until
    }


def all_session_stats() -> Dict[str, dict]:
    """
    Return session stats for all symbols with >= 1 trade this session.
    Suitable for dashboards and API endpoints.
    """
    return {
        sym: {
            "wins":              _symbol_wins.get(sym, 0),
            "losses":            _session_losses.get(sym, 0),
            "trades":            _symbol_trades.get(sym, 0),
            "win_rate":          round(get_symbol_score(sym), 4),
            "suspended":         is_suspended(sym),
            "minutes_remaining": round(get_suspension_remaining_minutes(sym), 2),
        }
        for sym in _symbol_trades
        if _symbol_trades[sym] > 0
    }


def get_session_losses(symbol: str) -> int:
    """Return the number of losses recorded for *symbol* this session."""
    return _session_losses.get(symbol, 0)


def is_dead_zone() -> bool:
    """
    Return True if the current UTC hour is in [00:00, 05:00).
    During this window all Boom/Crash symbols are excluded from get_queue().
    """
    hour: int = datetime.datetime.utcnow().hour
    dead_start: int = getattr(config, "DEAD_ZONE_START_UTC", 0)
    dead_end:   int = getattr(config, "DEAD_ZONE_END_UTC",   5)
    return dead_start <= hour < dead_end


# ─────────────────────────────────────────────────────────────────────────────
# Compat shims for callers using the old active-list API
# ─────────────────────────────────────────────────────────────────────────────

_active_managed: Set[str] = set(ALL_MANAGED_SYMBOLS)


def update_active(symbols: List[str]) -> None:
    """
    Refresh the active symbol set from an external source (e.g. Deriv API response).
    Volatility indices are unconditionally forced in regardless of API response.
    """
    global _active_managed
    _active_managed = set(symbols) | _VOLATILITY_SET
    logger.info(
        f"update_active: {len(_active_managed)} active symbols "
        f"({len(symbols)} from API + {len(_VOLATILITY_SET)} volatility forced)"
    )


def decrement_all() -> None:
    """NO-OP — retained for backward compatibility. Suspension is unix-timestamp-based."""
    logger.debug("decrement_all() called — no-op (unix-timestamp suspension)")


def decrement_suspensions() -> None:
    """NO-OP — retained for backward compatibility. Alias for decrement_all()."""
    decrement_all()


# Per-cycle used-tracking (for callers that need within-cycle dedup)
_cycle_used: Set[str] = set()


def reset_cycle_used() -> None:
    """Clear the per-cycle 'used' set. Call at the start of each cycle."""
    global _cycle_used
    _cycle_used = set()


def is_used(symbol: str) -> bool:
    """Return True if *symbol* has already been traded this cycle."""
    return symbol in _cycle_used


def mark_used(symbol: str) -> None:
    """Mark *symbol* as used for the current cycle."""
    _cycle_used.add(symbol)


def record_trade(symbol: str, won: bool, pnl: float = 0.0) -> None:
    """Legacy alias for record_result(). pnl kwarg accepted but unused."""
    record_result(symbol=symbol, won=won)


def win_rate(symbol: str) -> float:
    """Alias for get_symbol_score()."""
    return get_symbol_score(symbol)


@property
def current_session_label() -> str:
    """Human-readable session label for dashboards. Does not affect trading logic."""
    hour = datetime.datetime.utcnow().hour
    if 0 <= hour < 5:   return "DEAD_ZONE"
    if 5 <= hour < 9:   return "ASIA"
    if 9 <= hour < 13:  return "LONDON"
    if 13 <= hour < 17: return "NEW_YORK"
    return "OVERLAP"


def log_session_stats() -> None:
    """On-demand session stats dump. Does not modify state."""
    traded_syms = [s for s in _symbol_trades if _symbol_trades[s] > 0]
    if not traded_syms:
        logger.info("SESSION STATS: no trades recorded this session")
        return
    traded_syms.sort(key=lambda s: -get_symbol_score(s))
    for sym in traded_syms:
        wins   = _symbol_wins.get(sym, 0)
        losses = _session_losses.get(sym, 0)
        trades = _symbol_trades.get(sym, 0)
        rate   = get_symbol_score(sym)
        logger.info(
            f"SESSION STATS: {sym} "
            f"W:{wins} L:{losses} T:{trades} Rate:{rate:.1%}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Midnight reset background task (module-level asyncio task)
# ─────────────────────────────────────────────────────────────────────────────

_reset_task: Optional[asyncio.Task] = None


async def _midnight_reset_loop() -> None:
    """
    Asyncio coroutine: sleep until next UTC midnight, fire reset_session(), loop.
    """
    while True:
        try:
            now_utc  = datetime.datetime.utcnow()
            tomorrow = (now_utc + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            sleep_sec = (tomorrow - now_utc).total_seconds()
            logger.info(
                f"Midnight-reset: next session reset in {sleep_sec:.0f}s "
                f"(at {tomorrow.isoformat()}Z)"
            )
            await asyncio.sleep(sleep_sec)
            logger.info("Midnight-reset: UTC midnight reached — resetting session")
            reset_session()

        except asyncio.CancelledError:
            logger.info("Midnight-reset task: cancelled cleanly")
            return

        except Exception as exc:
            logger.error(
                f"Midnight-reset task non-fatal error: {exc!r} — retrying in 60s"
            )
            await asyncio.sleep(60)


def start_midnight_reset_task(
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> asyncio.Task:
    """
    Schedule the midnight session-reset coroutine on the event loop.
    Must be called after the asyncio event loop is running.
    """
    global _reset_task
    if loop is None:
        loop = asyncio.get_event_loop()
    _reset_task = loop.create_task(
        _midnight_reset_loop(),
        name="symbol_manager_midnight_reset",
    )
    logger.info("Midnight-reset task started (fires daily at UTC 00:00)")
    return _reset_task


def stop_midnight_reset_task() -> None:
    """Cancel the background midnight-reset task. No-op if never started or already done."""
    global _reset_task
    if _reset_task and not _reset_task.done():
        _reset_task.cancel()
        logger.info("Midnight-reset task: stop requested")


# ─────────────────────────────────────────────────────────────────────────────
# SymbolManager class — thin wrapper over module-level functions for callers
# that import the class directly (backward compatibility with bot_engine.py)
# ─────────────────────────────────────────────────────────────────────────────

class SymbolManager:
    """
    Thin class wrapper over module-level state and functions.
    All state lives at module level — multiple SymbolManager instances
    share the same underlying dicts (singleton pattern).

    Callers using SymbolManager() instances continue to work without changes.
    """

    # -- Suspension --
    def suspend(self, symbol: str, minutes: float) -> None:
        suspend(symbol, minutes)

    def is_suspended(self, symbol: str) -> bool:
        return is_suspended(symbol)

    def get_suspension_remaining(self, symbol: str) -> float:
        return get_suspension_remaining(symbol)

    def get_suspension_remaining_minutes(self, symbol: str) -> float:
        return get_suspension_remaining_minutes(symbol)

    def all_suspension_status(self) -> Dict[str, float]:
        return all_suspension_status()

    # -- Trade lifecycle --
    def can_trade_now(self, symbol: str) -> bool:
        return can_trade_now(symbol)

    def record_trade_placed(self, symbol: str) -> None:
        record_trade_placed(symbol)

    def record_contract_opened(self, symbol: str) -> None:
        record_contract_opened(symbol)

    def record_contract_closed(self, symbol: str) -> None:
        record_contract_closed(symbol)

    # -- Results --
    def record_result(self, symbol: str, won: bool, pnl: float = 0.0) -> None:
        record_result(symbol, won)

    def record_trade(self, symbol: str, won: bool, pnl: float = 0.0) -> None:
        record_result(symbol, won)

    def get_session_losses(self, symbol: str) -> int:
        return get_session_losses(symbol)

    # -- Scoring --
    def get_symbol_score(self, symbol: str) -> float:
        return get_symbol_score(symbol)

    def win_rate(self, symbol: str) -> float:
        return get_symbol_score(symbol)

    def best_symbols(self, n: int) -> List[dict]:
        return best_symbols(n)

    # -- Queue --
    def get_queue(self, active_list: Optional[List[str]] = None,
                  max_symbols: Optional[int] = None) -> List[str]:
        if active_list is None:
            active_list = list(_active_managed)
        result = get_queue(active_list)
        if max_symbols is not None:
            result = result[:max_symbols]
        return result

    def update_active(self, symbols: List[str]) -> None:
        update_active(symbols)

    # -- Session --
    def is_in_session(self, symbol: str) -> bool:
        return is_in_session(symbol)

    def is_dead_zone(self) -> bool:
        return is_dead_zone()

    def reset_session(self) -> None:
        reset_session()

    def log_session_stats(self) -> None:
        log_session_stats()

    def all_session_stats(self) -> Dict[str, dict]:
        return all_session_stats()

    # -- Timing helpers --
    def time_since_last_trade(self, symbol: str) -> float:
        return time_since_last_trade(symbol)

    def gap_remaining_seconds(self, symbol: str) -> float:
        return gap_remaining_seconds(symbol)

    # -- Compat no-ops --
    def decrement_all(self) -> None:
        decrement_all()

    def decrement_suspensions(self) -> None:
        decrement_all()

    def reset_cycle_used(self) -> None:
        reset_cycle_used()

    def is_used(self, symbol: str) -> bool:
        return is_used(symbol)

    def mark_used(self, symbol: str) -> None:
        mark_used(symbol)

    @property
    def current_session(self) -> str:
        hour = datetime.datetime.utcnow().hour
        if 0 <= hour < 5:   return "DEAD_ZONE"
        if 5 <= hour < 9:   return "ASIA"
        if 9 <= hour < 13:  return "LONDON"
        if 13 <= hour < 17: return "NEW_YORK"
        return "OVERLAP"

    # -- Midnight reset task --
    def start_midnight_reset_task(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> asyncio.Task:
        return start_midnight_reset_task(loop)

    def stop_midnight_reset_task(self) -> None:
        stop_midnight_reset_task()

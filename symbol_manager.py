import time
import logging
from datetime import datetime, timezone
from typing import List, Dict
import config

logger = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────
_suspension_until: Dict[str, float] = {}
_last_traded: Dict[str, float]      = {}
_session_losses: Dict[str, int]     = {}
_session_wins: Dict[str, int]       = {}
_session_trades: Dict[str, int]     = {}
_active_symbols: set                = set()
_all_active: List[str]              = []


def suspend(symbol: str, minutes: float) -> None:
    until = time.time() + (minutes * 60)
    _suspension_until[symbol] = until
    logger.info(
        f"SUSPENDED: {symbol} for {minutes}min "
        f"expires at {datetime.fromtimestamp(until, tz=timezone.utc).strftime('%H:%M:%S')} UTC"
    )


def is_suspended(symbol: str) -> bool:
    until = _suspension_until.get(symbol, 0)
    if time.time() < until:
        remaining = (until - time.time()) / 60
        logger.debug(f"SUSPENDED: {symbol} {remaining:.1f}min remaining")
        return True
    return False


def can_trade_now(symbol: str) -> bool:
    if is_suspended(symbol):
        logger.info(f"BLOCKED: {symbol} is suspended")
        return False
    if symbol in _active_symbols:
        logger.info(f"BLOCKED: {symbol} already has an active contract")
        return False
    gap = time.time() - _last_traded.get(symbol, 0)
    if gap < config.SYMBOL_MIN_GAP_MINS * 60:
        remaining = (config.SYMBOL_MIN_GAP_MINS * 60 - gap) / 60
        logger.info(f"BLOCKED: {symbol} gap cooldown {remaining:.1f}min remaining")
        return False
    return True


def record_trade_placed(symbol: str) -> None:
    _last_traded[symbol] = time.time()
    _active_symbols.add(symbol)
    logger.info(f"TRADE PLACED: {symbol} | Active: {_active_symbols}")


def record_contract_closed(symbol: str) -> None:
    _active_symbols.discard(symbol)
    logger.info(f"CONTRACT CLOSED: {symbol} | Active: {_active_symbols}")


def record_result(symbol: str, won: bool) -> None:
    _session_trades[symbol] = _session_trades.get(symbol, 0) + 1
    if won:
        _session_wins[symbol] = _session_wins.get(symbol, 0) + 1
        suspend(symbol, config.SYMBOL_WIN_SUSPEND_MINS)
    else:
        _session_losses[symbol] = _session_losses.get(symbol, 0) + 1
        if _session_losses[symbol] >= config.SYMBOL_SESSION_BAN_LOSSES:
            suspend(symbol, config.SYMBOL_SESSION_BAN_MINS)
            logger.warning(
                f"SESSION BAN: {symbol} "
                f"{config.SYMBOL_SESSION_BAN_LOSSES} losses"
            )
        else:
            suspend(symbol, config.SYMBOL_LOSS_SUSPEND_MINS)


def get_symbol_score(symbol: str) -> float:
    trades = _session_trades.get(symbol, 0)
    wins   = _session_wins.get(symbol, 0)
    return wins / trades if trades > 0 else 0.5


def is_in_session(symbol: str) -> bool:
    hour = datetime.now(timezone.utc).hour
    if config.DEAD_ZONE_START_UTC <= hour < config.DEAD_ZONE_END_UTC:
        if symbol in config.BOOM_CRASH_SYMBOLS + config.JUMP_SYMBOLS:
            return False
    if symbol in ["BOOM500", "BOOM300N", "BOOM300"]:
        return config.BOOM500_START_UTC <= hour < config.BOOM500_END_UTC
    if symbol in ["CRASH500", "CRASH300N", "CRASH300"]:
        return config.CRASH500_START_UTC <= hour < config.CRASH500_END_UTC
    if symbol in ["BOOM1000", "CRASH1000", "BOOM150", "CRASH150"]:
        return config.BOOM_CRASH_1000_START <= hour < config.BOOM_CRASH_1000_END
    if symbol in config.JUMP_SYMBOLS:
        return config.JUMP_START_UTC <= hour < config.JUMP_END_UTC
    return True  # digits, range break, step, drift always available


def update_active(symbol_list: List[str]) -> None:
    global _all_active
    _all_active = [s for s in symbol_list if s in config.ALL_TRADE_SYMBOLS]
    logger.info(f"Active symbol pool: {len(_all_active)} symbols")


def get_queue() -> List[str]:
    available    = []
    suspended_log = []
    gap_log      = []
    session_log  = []

    for symbol in _all_active:
        if is_suspended(symbol):
            until = _suspension_until.get(symbol, 0)
            remaining = max(0, (until - time.time()) / 60)
            suspended_log.append(f"{symbol}({remaining:.1f}min)")
            continue
        if symbol in _active_symbols:
            continue
        if not is_in_session(symbol):
            session_log.append(symbol)
            continue
        gap = time.time() - _last_traded.get(symbol, 0)
        if gap < config.SYMBOL_MIN_GAP_MINS * 60:
            remaining = (config.SYMBOL_MIN_GAP_MINS * 60 - gap) / 60
            gap_log.append(f"{symbol}({remaining:.1f}min)")
            continue
        available.append(symbol)

    logger.info(
        f"QUEUE: {len(available)} available | "
        f"Suspended: [{', '.join(suspended_log)}] | "
        f"Gap: [{', '.join(gap_log)}] | "
        f"Session-blocked: {len(session_log)}"
    )
    return available


def best_symbols(n: int) -> list:
    scored = [
        {
            "symbol":   s,
            "trades":   _session_trades.get(s, 0),
            "win_rate": round(get_symbol_score(s) * 100, 1),
            "pnl":      0,
            "score":    get_symbol_score(s),
        }
        for s in _all_active if _session_trades.get(s, 0) > 0
    ]
    return sorted(scored, key=lambda x: x["score"], reverse=True)[:n]


def reset_session() -> None:
    _session_losses.clear()
    _session_wins.clear()
    _session_trades.clear()
    logger.info("Session counters reset at UTC midnight")


def get_suspended_list() -> list:
    now = time.time()
    return [
        {
            "symbol":  s,
            "minutes": round((until - now) / 60, 1),
        }
        for s, until in _suspension_until.items()
        if now < until
    ]


# ── Compatibility shim ────────────────────────────────────────
# bot_engine.py does: from symbol_manager import SymbolManager
# This thin wrapper delegates every call to the module functions above
# so both import styles work without any changes to other files.

class SymbolManager:
    """Thin class wrapper around module-level functions.
    Allows ``from symbol_manager import SymbolManager`` to keep working."""

    def suspend(self, symbol: str, minutes: float) -> None:
        suspend(symbol, minutes)

    def is_suspended(self, symbol: str) -> bool:
        return is_suspended(symbol)

    def can_trade_now(self, symbol: str) -> bool:
        return can_trade_now(symbol)

    def record_trade_placed(self, symbol: str) -> None:
        record_trade_placed(symbol)

    def record_contract_closed(self, symbol: str) -> None:
        record_contract_closed(symbol)

    def record_result(self, symbol: str, won: bool) -> None:
        record_result(symbol, won)

    def get_symbol_score(self, symbol: str) -> float:
        return get_symbol_score(symbol)

    def is_in_session(self, symbol: str) -> bool:
        return is_in_session(symbol)

    def update_active(self, symbol_list: List[str]) -> None:
        update_active(symbol_list)

    def get_queue(self) -> List[str]:
        return get_queue()

    def best_symbols(self, n: int) -> list:
        return best_symbols(n)

    def reset_session(self) -> None:
        reset_session()

    def get_suspended_list(self) -> list:
        return get_suspended_list()

    def decrement_suspensions(self) -> None:
        """No-op: unix-timestamp suspensions expire automatically via time.time()
        comparisons. This method exists solely for bot_engine.py compatibility.
        Also prunes stale keys to keep the dict tidy."""
        now = time.time()
        expired = [s for s, until in list(_suspension_until.items()) if until <= now]
        for s in expired:
            del _suspension_until[s]

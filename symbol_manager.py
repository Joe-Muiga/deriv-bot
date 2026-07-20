import time
import logging
from datetime import datetime, timezone
import config

logger = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────
_suspension_until: dict = {}    # symbol -> unix expiry timestamp
_last_traded: dict = {}         # symbol -> unix timestamp of last placement
_session_losses: dict = {}      # symbol -> loss count this UTC session
_symbol_wins: dict = {}         # symbol -> win count this session
_symbol_trades: dict = {}       # symbol -> total trade count this session
_active_symbols: set = set()    # symbols with currently open contracts
_jump_last_seen: dict = {}      # symbol -> unix timestamp of last detected jump

# ── Symbol category groupings (fallback to config if defined there) ──
_BOOM_CRASH_500_300 = getattr(config, 'BOOM_CRASH_500_300', ["BOOM500", "BOOM300", "CRASH300"])
_CRASH500_ONLY = getattr(config, 'CRASH500_ONLY', ["CRASH500"])
_BOOM_CRASH_1000 = getattr(config, 'BOOM_CRASH_1000', ["BOOM1000", "CRASH1000"])
_RANGE_BREAK_SYMBOLS = getattr(config, 'RANGE_BREAK_SYMBOLS', [])
_DIGIT_SYMBOLS = getattr(config, 'DIGIT_SYMBOLS', [])
_MEAN_REVERSION_SYMBOLS = getattr(config, 'MEAN_REVERSION_SYMBOLS', getattr(config, 'MEAN_REVERSION', []))
_STEP_SYMBOLS = getattr(config, 'STEP_SYMBOLS', [])
_JUMP_SYMBOLS = getattr(config, 'JUMP_SYMBOLS', [])

_BOOM_CRASH_ALL = set(_BOOM_CRASH_500_300) | set(_CRASH500_ONLY) | set(_BOOM_CRASH_1000)


def suspend(symbol: str, minutes: float) -> None:
    until = time.time() + (minutes * 60)
    _suspension_until[symbol] = until
    logger.info(
        f"SUSPENDED: {symbol} for {minutes}min "
        f"expires at {datetime.fromtimestamp(until, tz=timezone.utc).strftime('%H:%M:%S')} UTC"
    )


def is_suspended(symbol: str) -> bool:
    until = _suspension_until.get(symbol, 0)
    now = time.time()
    if now < until:
        remaining = until - now
        logger.debug(f"SUSPENDED: {symbol} {remaining:.0f}s remaining")
        return True
    return False


def can_trade_now(symbol: str) -> bool:
    if is_suspended(symbol):
        remaining = _suspension_until.get(symbol, 0) - time.time()
        logger.info(f"BLOCKED: {symbol} suspended ({remaining:.0f}s remaining)")
        return False

    if symbol in _active_symbols:
        logger.info(f"BLOCKED: {symbol} already has an active contract")
        return False

    gap_required = config.SYMBOL_MIN_GAP_MINS * 60
    elapsed = time.time() - _last_traded.get(symbol, 0)
    if elapsed < gap_required:
        logger.info(
            f"BLOCKED: {symbol} min-gap not met "
            f"({elapsed:.0f}s elapsed, {gap_required:.0f}s required)"
        )
        return False

    return True


def record_trade_placed(symbol: str) -> None:
    _last_traded[symbol] = time.time()
    _active_symbols.add(symbol)
    logger.info(f"TRADE PLACED: {symbol} | Active: {_active_symbols}")


def record_contract_opened(symbol: str) -> None:
    _active_symbols.add(symbol)
    logger.info(f"CONTRACT OPENED: {symbol} | Active: {_active_symbols}")


def record_contract_closed(symbol: str) -> None:
    _active_symbols.discard(symbol)
    logger.info(f"CONTRACT CLOSED: {symbol} | Active: {_active_symbols}")


def record_result(symbol: str, won: bool) -> None:
    _symbol_trades[symbol] = _symbol_trades.get(symbol, 0) + 1

    if won:
        _symbol_wins[symbol] = _symbol_wins.get(symbol, 0) + 1
        suspend(symbol, config.SYMBOL_WIN_SUSPEND_MINS)
        logger.info(f"RESULT: {symbol} WON | win-suspend applied")
    else:
        _session_losses[symbol] = _session_losses.get(symbol, 0) + 1
        loss_count = _session_losses[symbol]

        if loss_count >= config.SYMBOL_SESSION_BAN_LOSSES:
            suspend(symbol, 59940)
            logger.warning(
                f"SESSION BAN: {symbol} hit {loss_count} losses "
                f"this session — banned for remainder of session"
            )
        else:
            suspend(symbol, config.SYMBOL_LOSS_SUSPEND_MINS)
            logger.info(f"RESULT: {symbol} LOST ({loss_count}) | loss-suspend applied")


def get_symbol_score(symbol: str) -> float:
    return _symbol_wins.get(symbol, 0) / max(_symbol_trades.get(symbol, 1), 1)


def best_symbols(n: int) -> list:
    scored = [
        {
            "symbol": s,
            "win_rate": round(get_symbol_score(s) * 100, 1),
            "trades": _symbol_trades.get(s, 0),
        }
        for s in _symbol_trades
    ]
    return sorted(scored, key=lambda x: x["win_rate"], reverse=True)[:n]


def is_in_session(symbol: str) -> bool:
    now_hour = datetime.now(timezone.utc).hour

    # Explicit dead zone: 00:00–05:00 UTC blocks all Boom/Crash symbols
    if symbol in _BOOM_CRASH_ALL and 0 <= now_hour < 5:
        logger.debug(f"SESSION: {symbol} blocked — dead zone (00:00-05:00 UTC)")
        return False

    if symbol in _BOOM_CRASH_500_300:
        ok = 7 <= now_hour < 12
        if not ok:
            logger.debug(f"SESSION: {symbol} outside window 07:00-12:00 UTC")
        return ok

    if symbol in _CRASH500_ONLY:
        ok = 7 <= now_hour < 16
        if not ok:
            logger.debug(f"SESSION: {symbol} outside window 07:00-16:00 UTC")
        return ok

    if symbol in _BOOM_CRASH_1000:
        ok = 5 <= now_hour < 20
        if not ok:
            logger.debug(f"SESSION: {symbol} outside window 05:00-20:00 UTC")
        return ok

    if symbol in _RANGE_BREAK_SYMBOLS:
        return True

    if symbol in _DIGIT_SYMBOLS or symbol in _MEAN_REVERSION_SYMBOLS:
        return True

    if symbol in _STEP_SYMBOLS:
        return True

    if symbol in _JUMP_SYMBOLS:
        # Preferred window 07:00-20:00 UTC, but always allowed
        if not (7 <= now_hour < 20):
            logger.debug(f"SESSION: {symbol} outside preferred window but still allowed")
        return True

    # Default: no restriction defined, allow
    return True


def get_queue(active_list: list) -> list:
    tradeable = []
    suspended_list = []
    session_blocked = []

    for symbol in active_list:
        if symbol not in config.TRADE_SYMBOLS:
            continue

        if is_suspended(symbol):
            suspended_list.append(symbol)
            continue

        if not is_in_session(symbol):
            session_blocked.append(symbol)
            continue

        if not can_trade_now(symbol):
            # can_trade_now already logs the specific reason
            continue

        tradeable.append(symbol)

    logger.info(
        f"Queue: {len(tradeable)} tradeable | "
        f"Suspended: {suspended_list} | "
        f"Session-blocked: {session_blocked}"
    )
    return tradeable


def reset_session() -> None:
    _session_losses.clear()
    _symbol_wins.clear()
    _symbol_trades.clear()
    logger.info("Session counters reset at UTC midnight (suspensions preserved)")


def get_suspended_list() -> list:
    now = time.time()
    return [
        {"symbol": s, "seconds": round(until - now, 1)}
        for s, until in _suspension_until.items()
        if now < until
    ]

"""
symbol_manager.py — per-symbol suspension, min-gap, and session gating.

DELETED (Sep 2026 pivot), not disabled: every synthetic-index-specific
session rule — the Boom/Crash 00:00-05:00 UTC dead zone and per-family
preferred windows (BOOM_CRASH_500_300/CRASH500_ONLY/BOOM_CRASH_1000), the
Bear/Bull ("Daily Reset") 00:00 GMT post-reset flag
(is_post_reset/get_bear_bull_state), and the Range Break/Digit/Mean-
Reversion/Step/Jump pass-through branches that existed only to keep those
now-deleted categories flowing through is_in_session() unblocked.

ADDED: real session gating for Forex/gold/commodities, which — unlike
synthetic indices — actually close (weekends, and briefly around the
daily rollover). This is a coarse, scan-time efficiency filter only; the
authoritative check is deriv_client.DerivClient._check_market_open(),
which queries Deriv's own active_symbols "is_open" flag live at buy time
and is what actually prevents an order from being placed while a market
is closed. is_in_session() here just avoids wasting scan cycles (and
noisy log lines) on a symbol that's obviously closed for the weekend.

CHANGED: record_result()'s per-symbol loss-suspension ladder, which used
to be intentionally left as bookkeeping-only ("not applied — symbol-wide
suspension disabled, see pair_suspension.py") because finer-grained
per-(indicator, symbol) suspension in pair_suspension.py handled it
instead. pair_suspension.py has been deleted along with the multi-
indicator pipeline it existed for — there's exactly one strategy now
(ICT/SMC), so per-(indicator, symbol) granularity isn't meaningful
anymore and plain per-symbol suspension is the right mechanism again.
Restored: a losing trade now actually calls self.suspend() using the
escalating ladder, same as it did before the popular-indicator pipeline
introduced pair-level suspension.
"""

import time
import logging
from datetime import datetime, timezone
import config

logger = logging.getLogger(__name__)


class SymbolManager:
    def __init__(self):
        self._suspension_until = {}    # symbol -> unix expiry timestamp
        self._last_traded = {}         # symbol -> unix timestamp of last placement
        self._session_losses = {}      # symbol -> consecutive loss count
        self._symbol_wins = {}         # symbol -> win count this session
        self._symbol_trades = {}       # symbol -> total trade count this session
        self._active_symbols = set()   # symbols with currently open contracts
        self._all_active = []
        self.current_session = "Active"

    def suspend(self, symbol: str, minutes: float) -> None:
        until = time.time() + (minutes * 60)
        self._suspension_until[symbol] = until
        logger.info(
            f"SUSPENDED: {symbol} for {minutes}min "
            f"expires at {datetime.fromtimestamp(until, tz=timezone.utc).strftime('%H:%M:%S')} UTC"
        )

    def is_suspended(self, symbol: str) -> bool:
        until = self._suspension_until.get(symbol, 0)
        now = time.time()
        if now < until:
            remaining = until - now
            logger.debug(f"SUSPENDED: {symbol} {remaining:.0f}s remaining")
            return True
        return False

    def can_trade_now(self, symbol: str) -> bool:
        if self.is_suspended(symbol):
            remaining = self._suspension_until.get(symbol, 0) - time.time()
            logger.info(f"BLOCKED: {symbol} suspended ({remaining:.0f}s remaining)")
            return False

        if symbol in self._active_symbols:
            logger.info(f"BLOCKED: {symbol} already has an active contract")
            return False

        gap_required = config.SYMBOL_MIN_GAP_MINS * 60
        elapsed = time.time() - self._last_traded.get(symbol, 0)
        if elapsed < gap_required:
            logger.info(
                f"BLOCKED: {symbol} min-gap not met "
                f"({elapsed:.0f}s elapsed, {gap_required:.0f}s required)"
            )
            return False

        if not self.is_in_session(symbol):
            logger.info(f"BLOCKED: {symbol} market closed (weekend/session)")
            return False

        return True

    def record_trade_placed(self, symbol: str) -> None:
        self._last_traded[symbol] = time.time()
        self._active_symbols.add(symbol)
        logger.info(f"TRADE PLACED: {symbol} | Active: {self._active_symbols}")

    def record_contract_opened(self, symbol: str) -> None:
        self._active_symbols.add(symbol)
        logger.info(f"CONTRACT OPENED: {symbol} | Active: {self._active_symbols}")

    def record_contract_closed(self, symbol: str) -> None:
        self._active_symbols.discard(symbol)
        logger.info(f"CONTRACT CLOSED: {symbol} | Active: {self._active_symbols}")

    def record_result(self, symbol: str, won: bool) -> None:
        self._symbol_trades[symbol] = self._symbol_trades.get(symbol, 0) + 1

        if won:
            self._symbol_wins[symbol] = self._symbol_wins.get(symbol, 0) + 1
            self._session_losses[symbol] = 0
            logger.info(f"RESULT: {symbol} WON | consecutive-loss count reset")
        else:
            self._session_losses[symbol] = self._session_losses.get(symbol, 0) + 1
            loss_count = self._session_losses[symbol]

            # Escalating ladder: 1st consecutive loss on a symbol (since its
            # last win, or since process start) -> ladder[0] minutes, 2nd ->
            # ladder[1], ..., loss_count beyond the ladder's length holds at
            # the ladder's last (highest) value. A win resets this to 0.
            ladder = getattr(config, "SESSION_LOSS_SUSPEND_LADDER_MINS", [60, 120, 180, 240])
            idx = min(loss_count, len(ladder)) - 1
            suspend_mins = ladder[idx]
            self.suspend(symbol, suspend_mins)
            logger.info(
                f"RESULT: {symbol} LOST ({loss_count} consecutive) | "
                f"suspended {suspend_mins}min"
            )

    def get_symbol_score(self, symbol: str) -> float:
        return self._symbol_wins.get(symbol, 0) / max(self._symbol_trades.get(symbol, 1), 1)

    def win_rate(self, symbol: str) -> float:
        """Alias used by bot_engine._composite_score()."""
        return self.get_symbol_score(symbol)

    def best_symbols(self, n: int) -> list:
        scored = [
            {
                "symbol": s,
                "win_rate": round(self.get_symbol_score(s) * 100, 1),
                "trades": self._symbol_trades.get(s, 0),
            }
            for s in self._symbol_trades
        ]
        return sorted(scored, key=lambda x: x["win_rate"], reverse=True)[:n]

    def is_in_session(self, symbol: str) -> bool:
        """
        Coarse weekend gate for Forex/gold/commodities. Real Forex/gold/oil
        trading runs continuously from roughly Sunday 21:00 UTC (Sydney
        open) to Friday 21:00 UTC (New York close) — approximated here
        with simple UTC weekday/hour checks rather than a precise
        per-symbol session calendar, since the authoritative gate is
        deriv_client._check_market_open()'s live API check at buy time
        (see module docstring); this only needs to be roughly right to
        save wasted scan cycles over the weekend.

        weekday(): Monday=0 .. Sunday=6.
        """
        now = datetime.now(timezone.utc)
        wd, hour = now.weekday(), now.hour

        if wd == 5:                       # all day Saturday
            return False
        if wd == 4 and hour >= 21:        # Friday after 21:00 UTC
            return False
        if wd == 6 and hour < 21:         # Sunday before 21:00 UTC
            return False

        return True

    def get_queue(self, active_list: list = None) -> list:
        # Use passed list, or fall back to _all_active, or fall back to config
        source = active_list or self._all_active
        if not source:
            source = list(getattr(config, 'ALL_TRADE_SYMBOLS',
                          getattr(config, 'TRADE_SYMBOLS',
                          getattr(config, 'ALL_SYMBOLS', []))))
            self._all_active = source
            logger.warning(
                f"get_queue: _all_active was empty — fell back to "
                f"config symbol list ({len(source)} symbols)"
            )

        tradeable = []
        suspended_list = []
        session_blocked = []

        now = time.time()
        for symbol in source:
            if symbol in self._active_symbols:
                continue

            if now < self._suspension_until.get(symbol, 0):
                remaining = (self._suspension_until[symbol] - now) / 60
                suspended_list.append(f"{symbol}({remaining:.1f}m)")
                continue

            if not self.is_in_session(symbol):
                session_blocked.append(symbol)
                continue

            gap_required = getattr(config, 'SYMBOL_MIN_GAP_MINS', 1) * 60
            elapsed = now - self._last_traded.get(symbol, 0)
            if elapsed < gap_required:
                continue

            tradeable.append(symbol)

        logger.info(
            f"Queue: {len(tradeable)} tradeable | "
            f"Suspended: {suspended_list} | "
            f"Session-blocked: {session_blocked}"
        )
        return tradeable

    def update_active(self, symbol_list):
        self._all_active = [
            s for s in symbol_list
            if s in getattr(config, 'ALL_TRADE_SYMBOLS',
                getattr(config, 'TRADE_SYMBOLS',
                getattr(config, 'ALL_SYMBOLS', symbol_list)))
        ]
        logger.info(f"Active pool: {len(self._all_active)} symbols")

    def reset_session(self) -> None:
        """Manual/administrative use only — see record_result()'s ladder
        docstring for why this isn't called automatically on any calendar
        boundary."""
        self._session_losses.clear()
        self._symbol_wins.clear()
        self._symbol_trades.clear()
        logger.info("Session counters reset (manual reset_session() call — suspensions preserved)")

    def get_suspended_list(self) -> list:
        now = time.time()
        return [
            {"symbol": s, "minutes": round((until - now) / 60, 1)}
            for s, until in self._suspension_until.items()
            if now < until
        ]

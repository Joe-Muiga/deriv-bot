"""
risk_manager.py – Phase C risk overlay for the SIFM bot.

v9 — Complete rewrite per architectural spec.

KEY CHANGES vs v8
─────────────────
• All loss-streak trading halts, pause cycles, quality gates, and
  confidence/strength gates REMOVED permanently.
• Daily loss limit logic REMOVED (lives in bot_engine only).
• can_trade() blocked only by: open_contracts, MIN_STAKE floor,
  DRAINING state. Never by loss streak.
• Reverse-Martingale only: multiplier resets to 1.0 on ANY loss,
  scales UP on consecutive wins only.
• Stake recalculated live from deriv_client.get_balance() every call —
  balance never cached more than 1 cycle old.
• Win-streak multiplier + concurrent-slot table:
    0–2 wins  → 1.0×, +0 slots
    3   wins  → 1.5×, +0 slots
    4–5 wins  → 2.0×, +2 slots
    6–7 wins  → 3.0×, +4 slots
    ≥8  wins  → 4.0×, +6 slots
• Hard cap: actual_stake ≤ MAX_STAKE, concurrent ≤ config ceiling.
• Post-close log line:
    STREAK: +{n} | MULTIPLIER: {m}× | NEXT STAKE: ${s:.2f} | CONCURRENT LIMIT: {c}
• All exposed properties and public API preserved/extended.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional

import config

logger = logging.getLogger(__name__)


# ── Bot state enum ────────────────────────────────────────────────────────────

class BotState(Enum):
    RUNNING  = auto()
    DRAINING = auto()
    STOPPED  = auto()


# ── Trade record ──────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    symbol:      str
    direction:   str
    stake:       float
    entry_price: float
    exit_price:  Optional[float] = None
    pnl:         float = 0.0
    won:         bool  = False
    timestamp:   float = field(default_factory=time.time)


# ── Win-streak tier table ─────────────────────────────────────────────────────
#
# Each row: (min_streak_inclusive, stake_multiplier, extra_concurrent_slots)
# Evaluated top-down; first matching row wins.

_STREAK_TABLE: list[tuple[int, float, int]] = [
    (8, 4.0, 6),
    (6, 3.0, 4),
    (4, 2.0, 2),
    (3, 1.5, 0),
    (0, 1.0, 0),   # 0–2 wins (or any loss streak → streak ≤ 0)
]


def _streak_tier(streak: int) -> tuple[float, int]:
    """
    Return (stake_multiplier, extra_concurrent_slots) for the given streak.
    Loss streaks (streak ≤ 0) always yield (1.0, 0).
    """
    if streak <= 0:
        return 1.0, 0
    for min_s, mult, bonus in _STREAK_TABLE:
        if streak >= min_s:
            return mult, bonus
    return 1.0, 0  # unreachable, satisfies type checker


# ── RiskManager ───────────────────────────────────────────────────────────────

class RiskManager:
    """
    Stateful risk and position-sizing layer for the SIFM bot.

    Thread/task safety
    ──────────────────
    All state mutations happen in coroutines that are serialised by the
    single asyncio event loop — no additional locking is needed.
    """

    def __init__(
        self,
        risk_per_trade: float = 0.01,
        min_stake:      float = 0.35,
        max_stake:      float = 500.0,
        max_concurrent: int   = 20,
        deriv_client=None,
    ):
        # ── Config ────────────────────────────────────────────────────────────
        self.risk_per_trade = risk_per_trade
        self.min_stake      = min_stake
        self.max_stake      = max_stake
        self.max_concurrent = max_concurrent   # base ceiling (wins may add slots)
        self._deriv_client  = deriv_client     # injected; must expose get_balance()

        # ── Live balance (refreshed every calculate_stake call) ───────────────
        self._current_balance: float = 0.0
        self._balance_cycle:   int   = -1      # monotonic cycle counter at last fetch
        self._current_cycle:   int   = 0       # incremented by bot_engine each scan

        # ── Day-reset bookkeeping ─────────────────────────────────────────────
        self._day_tag:           str   = ""
        self._day_start_balance: float = 0.0

        # ── Streak ────────────────────────────────────────────────────────────
        # positive  = consecutive wins
        # negative  = consecutive losses
        # reset to 0 on day rollover
        self._current_streak: int = 0

        # ── Concurrent trade tracking ─────────────────────────────────────────
        self._open_trade_count: int = 0

        # ── Bot state ─────────────────────────────────────────────────────────
        self._bot_state: BotState = BotState.RUNNING

        # ── Pause-cycle countdown ──────────────────────────────────────────────
        # bot_engine calls decrement_pause() / consume_pause_cycle() once per
        # cycle; the main loop reads pause_cycles_remaining to decide whether
        # to skip trading this cycle.
        self._pause_cycles_remaining: int = 0

        # ── Session stats ─────────────────────────────────────────────────────
        self.total_trades: int   = 0
        self.wins:         int   = 0
        self.losses:       int   = 0
        self.total_pnl:    float = 0.0
        self._trades: List[TradeRecord] = []

    # ── Bot-state management ──────────────────────────────────────────────────

    def set_bot_state(self, state: BotState) -> None:
        self._bot_state = state
        logger.info(f"Bot state → {state.name}")

    @property
    def bot_state(self) -> BotState:
        return self._bot_state

    # ── Cycle clock (bot_engine calls this once per scan iteration) ───────────

    def tick_cycle(self) -> None:
        """Advance the internal cycle counter by one scan cycle."""
        self._current_cycle += 1

    # ── Pause-cycle management ────────────────────────────────────────────────

    @property
    def pause_cycles_remaining(self) -> int:
        """
        Number of cycles the bot should skip trading.
        Always returns 0 when not in a paused state — never raises, never None.
        """
        return self._pause_cycles_remaining

    def decrement_pause(self) -> None:
        """Called once per cycle by bot_engine to count down any active pause."""
        if self._pause_cycles_remaining > 0:
            self._pause_cycles_remaining -= 1

    def consume_pause_cycle(self) -> None:
        """
        Alias for decrement_pause() — satisfies bot_engine.py line 379 which
        calls self.risk.consume_pause_cycle() inside the pause-skip branch.
        """
        self.decrement_pause()

    # ── Balance management ────────────────────────────────────────────────────

    def set_balance(self, balance: float) -> None:
        """
        Synchronous balance update (used during initialisation / event callbacks).
        Marks the balance as fresh for the current cycle.
        """
        self._current_balance = balance
        self._balance_cycle   = self._current_cycle
        self._handle_day_rollover(balance)

    async def _fetch_live_balance(self) -> float:
        """
        Fetch balance from deriv_client if it hasn't been fetched this cycle,
        or if no client is injected fall back to the last known value.
        Always returns a non-negative float; never raises.
        """
        if self._deriv_client is None:
            return self._current_balance

        # Already fresh for this cycle — skip network round-trip
        if self._balance_cycle == self._current_cycle and self._current_balance > 0:
            return self._current_balance

        try:
            balance = await self._deriv_client.get_balance()
            if balance is not None and balance >= 0:
                self._current_balance = float(balance)
                self._balance_cycle   = self._current_cycle
                self._handle_day_rollover(self._current_balance)
        except Exception:
            pass  # silent exit — return stale value

        return self._current_balance

    def _handle_day_rollover(self, balance: float) -> None:
        today = _today_tag()
        if today != self._day_tag:
            self._day_tag           = today
            self._current_streak    = 0
            self._day_start_balance = balance if balance > 0 else self._day_start_balance
            logger.info(
                f"New trading day {today} | "
                f"Starting balance: ${self._day_start_balance:.4f}")
        if self._day_start_balance == 0.0 and balance > 0:
            self._day_start_balance = balance
            logger.info(f"Day-start balance initialised: ${balance:.4f}")

    # ── Streak properties ─────────────────────────────────────────────────────

    @property
    def current_streak(self) -> int:
        """Positive = consecutive wins, negative = consecutive losses."""
        return self._current_streak

    @property
    def win_streak(self) -> int:
        """Current consecutive-win count (0 when on a loss streak or neutral)."""
        return max(0, self._current_streak)

    @property
    def loss_streak(self) -> int:
        """Current consecutive-loss count as a positive integer (0 when winning)."""
        return max(0, -self._current_streak)

    # ── Multiplier / concurrent properties ───────────────────────────────────

    @property
    def current_multiplier(self) -> float:
        """Stake multiplier driven by win streak only."""
        mult, _ = _streak_tier(self._current_streak)
        return mult

    @property
    def current_concurrent_limit(self) -> int:
        """
        Effective concurrent-trade ceiling = base + win-streak bonus,
        hard-capped at the absolute config ceiling.
        """
        _, bonus = _streak_tier(self._current_streak)
        ceiling = getattr(config, "MAX_CONCURRENT_TRADES", self.max_concurrent)
        return min(self.max_concurrent + bonus, ceiling)

    # ── Stake calculation ─────────────────────────────────────────────────────

    async def calculate_stake(self) -> float:
        """
        Compute the actual stake for the next trade.

        Formula
        ───────
        base_stake   = RISK_PER_TRADE_PCT × live_balance   (recalculated live)
        actual_stake = base_stake × current_multiplier
        actual_stake = clamp(actual_stake, MIN_STAKE, MAX_STAKE)

        On a loss streak current_multiplier == 1.0 so base_stake is used as-is.
        Never Martingale (no doubling after loss).
        """
        balance = await self._fetch_live_balance()

        if balance <= 0:
            return self.min_stake

        base_stake   = balance * self.risk_per_trade
        actual_stake = base_stake * self.current_multiplier
        actual_stake = min(actual_stake, self.max_stake)
        actual_stake = max(actual_stake, self.min_stake)
        return round(actual_stake, 2)

    # ── can_trade gate ────────────────────────────────────────────────────────

    def can_trade(self) -> bool:
        """
        Return True iff ALL of:
          1. open_contracts < current_concurrent_limit
          2. current_balance >= MIN_STAKE
          3. bot_state != DRAINING

        Explicitly NOT gated by loss streak — symbol suspension in
        bot_engine handles loss-based cooldowns.
        """
        if self._bot_state == BotState.DRAINING:
            logger.debug("can_trade: bot in DRAINING state")
            return False

        if self._current_balance < self.min_stake:
            logger.debug(
                f"can_trade: balance ${self._current_balance:.4f} "
                f"< MIN_STAKE ${self.min_stake:.2f}")
            return False

        if self._open_trade_count >= self.current_concurrent_limit:
            logger.debug(
                f"can_trade: concurrent limit reached "
                f"({self._open_trade_count}/{self.current_concurrent_limit})")
            return False

        return True

    # ── Result recording ──────────────────────────────────────────────────────

    def record_result(self, won: bool) -> None:
        """
        Update streak and immediately log the post-trade summary line.

        Rules
        ─────
        • Win  → increment win streak; preserve direction.
        • Loss → reset multiplier to 1.0 IMMEDIATELY (streak → -1 or more negative);
                 reset concurrent bonus to base.
        • Never Martingale — any loss resets to base, never doubles.
        """
        if won:
            self.wins        += 1
            # Move streak in positive direction
            self._current_streak = max(0, self._current_streak) + 1
        else:
            self.losses      += 1
            # Loss: multiplier instantly back to 1.0 (streak goes negative)
            self._current_streak = min(0, self._current_streak) - 1

        # Compute next-trade parameters for log line (uses updated streak)
        next_multiplier = self.current_multiplier
        next_concurrent = self.current_concurrent_limit

        # Compute next stake synchronously with current cached balance
        # (full async refresh happens inside calculate_stake at trade time)
        balance = self._current_balance if self._current_balance > 0 else self.min_stake
        base    = balance * self.risk_per_trade
        raw     = base * next_multiplier
        raw     = min(raw, self.max_stake)
        raw     = max(raw, self.min_stake)
        next_stake = round(raw, 2)

        if won:
            logger.info(
                f"STREAK: +{self.win_streak} | "
                f"MULTIPLIER: {next_multiplier:.1f}× | "
                f"NEXT STAKE: ${next_stake:.2f} | "
                f"CONCURRENT LIMIT: {next_concurrent}")
        else:
            logger.info(
                f"STREAK: -{self.loss_streak} | "
                f"MULTIPLIER: {next_multiplier:.1f}× | "
                f"NEXT STAKE: ${next_stake:.2f} | "
                f"CONCURRENT LIMIT: {next_concurrent}")

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    def register_open(
        self,
        symbol:      str,
        direction:   str,
        stake:       float,
        entry_price: float,
    ) -> TradeRecord:
        rec = TradeRecord(
            symbol=symbol,
            direction=direction,
            stake=stake,
            entry_price=entry_price,
        )
        self._trades.append(rec)
        self._open_trade_count += 1
        self.total_trades      += 1
        logger.info(
            f"Trade OPEN  | {symbol} {direction} | "
            f"stake=${stake:.2f} | price={entry_price} | "
            f"streak={self._current_streak} | "
            f"multiplier={self.current_multiplier:.1f}× | "
            f"concurrent={self._open_trade_count}/{self.current_concurrent_limit}")
        return rec

    def register_close(
        self,
        rec:        TradeRecord,
        exit_price: float,
        pnl:        float,
    ) -> None:
        rec.exit_price          = exit_price
        rec.pnl                 = pnl
        rec.won                 = pnl > 0
        self._open_trade_count  = max(0, self._open_trade_count - 1)
        self.total_pnl         += pnl

        # Record result → updates streak and emits required log line
        self.record_result(rec.won)

        logger.info(
            f"Trade CLOSE | {rec.symbol} {rec.direction} | "
            f"pnl=${pnl:+.4f} | {'WIN' if rec.won else 'LOSS'} | "
            f"streak={self._current_streak} | "
            f"balance=${self._current_balance:.4f} | "
            f"open={self._open_trade_count}")

    # ── Session helpers ───────────────────────────────────────────────────────

    @property
    def current_balance(self) -> float:
        return self._current_balance

    @property
    def day_start_balance(self) -> float:
        return self._day_start_balance

    @property
    def daily_pnl(self) -> float:
        return self._current_balance - self._day_start_balance

    @property
    def daily_pnl_pct(self) -> float:
        if self._day_start_balance == 0:
            return 0.0
        return self.daily_pnl / self._day_start_balance

    @staticmethod
    def minutes_until_midnight() -> float:
        now      = datetime.datetime.utcnow()
        midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return (midnight - now).total_seconds() / 60

    def summary(self) -> dict:
        total = self.wins + self.losses
        return {
            "current_balance":      round(self._current_balance, 4),
            "day_start_balance":    round(self._day_start_balance, 4),
            "daily_pnl":            round(self.daily_pnl, 4),
            "daily_pnl_pct":        round(self.daily_pnl_pct * 100, 2),
            "total_trades":         self.total_trades,
            "wins":                 self.wins,
            "losses":               self.losses,
            "win_rate":             round(self.wins / total * 100, 1) if total else 0.0,
            "total_pnl":            round(self.total_pnl, 4),
            "open_trades":          self._open_trade_count,
            "streak":               self._current_streak,
            "win_streak":           self.win_streak,
            "loss_streak":          self.loss_streak,
            "multiplier":           self.current_multiplier,
            "concurrent_limit":     self.current_concurrent_limit,
            "bot_state":            self._bot_state.name,
        }


# ── Module-level helpers ──────────────────────────────────────────────────────

def _today_tag() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

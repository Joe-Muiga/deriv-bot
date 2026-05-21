"""
risk_manager.py – Risk and position-sizing layer.
v11 — Compounding base stake. Win-streak multipliers. No external trade gates.

Design:
  Base stake = BASE_STAKE_PCT × current_live_balance (recalculated every trade).
  Win-streak multiplier applied on top.
  Any loss: streak → 0, multiplier → 1×, next base from current balance.
  can_trade(): blocked only by open >= limit OR balance < MIN_STAKE.
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


class BotState(Enum):
    RUNNING  = auto()
    DRAINING = auto()
    STOPPED  = auto()


@dataclass
class TradeRecord:
    symbol      : str
    direction   : str
    stake       : float
    entry_price : float
    exit_price  : Optional[float] = None
    pnl         : float = 0.0
    won         : bool  = False
    timestamp   : float = field(default_factory=time.time)


def _streak_tier(streak: int) -> tuple:
    """Return (stake_multiplier, extra_concurrent_slots) for streak."""
    if streak <= 0:
        return 1.0, 0
    thresholds  = getattr(config, "WIN_STREAK_THRESHOLDS",  [3, 5, 8, 12])
    multipliers = getattr(config, "WIN_STREAK_MULTIPLIERS", [1.5, 2.0, 3.0, 4.0])
    extra_slots = getattr(config, "WIN_STREAK_EXTRA_SLOTS", [2, 4, 6, 8])
    for i in range(len(thresholds) - 1, -1, -1):
        if streak >= thresholds[i]:
            return multipliers[i], extra_slots[i]
    return 1.0, 0


class RiskManager:

    def __init__(
        self,
        risk_per_trade : float = None,
        min_stake      : float = None,
        max_stake      : float = None,
        max_concurrent : int   = None,
        deriv_client   = None,
    ):
        self.base_stake_pct = getattr(config, "BASE_STAKE_PCT", 0.01)
        self.risk_per_trade = risk_per_trade or self.base_stake_pct
        self.min_stake      = min_stake  or getattr(config, "MIN_STAKE",             0.35)
        self.max_stake      = max_stake  or getattr(config, "MAX_STAKE",             50.0)
        self.max_concurrent = max_concurrent or getattr(config, "MAX_CONCURRENT_TRADES", 10)
        self._deriv_client  = deriv_client

        self._current_balance : float = 0.0
        self._balance_cycle   : int   = -1
        self._current_cycle   : int   = 0

        self._day_tag           : str   = ""
        self._day_start_balance : float = 0.0

        self._current_streak    : int   = 0
        self._open_trade_count  : int   = 0
        self._bot_state         : BotState = BotState.RUNNING
        self._pause_cycles_remaining : int = 0

        self.total_trades : int   = 0
        self.wins         : int   = 0
        self.losses       : int   = 0
        self.total_pnl    : float = 0.0
        self._trades : List[TradeRecord] = []

    # ── Bot state ─────────────────────────────────────────────────────────────

    def set_bot_state(self, state: BotState) -> None:
        self._bot_state = state
        logger.info(f"Bot state → {state.name}")

    @property
    def bot_state(self) -> BotState:
        return self._bot_state

    def tick_cycle(self) -> None:
        self._current_cycle += 1

    @property
    def pause_cycles_remaining(self) -> int:
        return self._pause_cycles_remaining

    def decrement_pause(self) -> None:
        if self._pause_cycles_remaining > 0:
            self._pause_cycles_remaining -= 1

    def consume_pause_cycle(self) -> None:
        self.decrement_pause()

    # ── Balance ───────────────────────────────────────────────────────────────

    def set_balance(self, balance: float) -> None:
        self._current_balance = balance
        self._balance_cycle   = self._current_cycle
        self._handle_day_rollover(balance)

    async def _fetch_live_balance(self) -> float:
        if self._deriv_client is None:
            return self._current_balance
        if self._balance_cycle == self._current_cycle and self._current_balance > 0:
            return self._current_balance
        try:
            balance = await self._deriv_client.get_balance()
            if balance is not None and balance >= 0:
                self._current_balance = float(balance)
                self._balance_cycle   = self._current_cycle
                self._handle_day_rollover(self._current_balance)
        except Exception:
            pass
        return self._current_balance

    def _handle_day_rollover(self, balance: float) -> None:
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._day_tag:
            self._day_tag           = today
            self._current_streak    = 0
            self._day_start_balance = balance if balance > 0 else self._day_start_balance
            logger.info(f"New trading day {today} | Starting balance: ${self._day_start_balance:.4f}")
        if self._day_start_balance == 0.0 and balance > 0:
            self._day_start_balance = balance

    # ── Streak ────────────────────────────────────────────────────────────────

    @property
    def current_streak(self) -> int:
        return self._current_streak

    @property
    def win_streak(self) -> int:
        return max(0, self._current_streak)

    @property
    def loss_streak(self) -> int:
        return max(0, -self._current_streak)

    @property
    def current_multiplier(self) -> float:
        mult, _ = _streak_tier(self._current_streak)
        return mult

    @property
    def current_concurrent_limit(self) -> int:
        _, bonus  = _streak_tier(self._current_streak)
        ceiling   = getattr(config, "MAX_CONCURRENT_TRADES", self.max_concurrent)
        return min(self.max_concurrent + bonus, ceiling)

    # ── Stake ─────────────────────────────────────────────────────────────────

    async def calculate_stake(self) -> float:
        """
        base_stake   = BASE_STAKE_PCT × live_balance
        actual_stake = base_stake × current_multiplier
        actual_stake = clamp(actual_stake, MIN_STAKE, MAX_STAKE)
        """
        balance = await self._fetch_live_balance()
        if balance <= 0:
            return self.min_stake
        base_stake   = balance * self.base_stake_pct
        multiplier   = self.current_multiplier
        actual_stake = max(self.min_stake, min(self.max_stake, round(base_stake * multiplier, 2)))
        logger.info(
            f"Stake: ${actual_stake:.2f} "
            f"(base=${base_stake:.2f} "
            f"streak={'+' if self._current_streak >= 0 else ''}{self._current_streak} "
            f"multiplier={multiplier:.1f}x)")
        return actual_stake

    # ── can_trade gate ────────────────────────────────────────────────────────

    def can_trade(self) -> bool:
        """
        True iff:
          1. open_contracts < current_concurrent_limit
          2. balance >= MIN_STAKE
          3. bot_state != DRAINING
        Never blocked for any other reason.
        """
        if self._bot_state == BotState.DRAINING:
            return False
        if self._current_balance < self.min_stake:
            return False
        if self._open_trade_count >= self.current_concurrent_limit:
            return False
        return True

    # ── Result recording ──────────────────────────────────────────────────────

    def record_result(self, won: bool) -> None:
        if won:
            self.wins            += 1
            self._current_streak  = max(0, self._current_streak) + 1
        else:
            self.losses          += 1
            self._current_streak  = 0   # any loss resets streak immediately

        mult, _ = _streak_tier(self._current_streak)
        balance = self._current_balance if self._current_balance > 0 else self.min_stake
        raw     = max(self.min_stake, min(self.max_stake, round(balance * self.base_stake_pct * mult, 2)))

        if won:
            logger.info(
                f"STREAK: +{self.win_streak} | MULTIPLIER: {mult:.1f}× | "
                f"NEXT STAKE: ${raw:.2f} | CONCURRENT LIMIT: {self.current_concurrent_limit}")
        else:
            logger.info(
                f"STREAK: 0 (reset) | MULTIPLIER: {mult:.1f}× | "
                f"NEXT STAKE: ${raw:.2f} | CONCURRENT LIMIT: {self.current_concurrent_limit}")

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    def register_open(self, symbol: str, direction: str,
                      stake: float, entry_price: float) -> TradeRecord:
        rec = TradeRecord(symbol=symbol, direction=direction,
                          stake=stake, entry_price=entry_price)
        self._trades.append(rec)
        self._open_trade_count += 1
        self.total_trades      += 1
        logger.info(
            f"Trade OPEN  | {symbol} {direction} | stake=${stake:.2f} | "
            f"price={entry_price} | streak={self._current_streak} | "
            f"multiplier={self.current_multiplier:.1f}× | "
            f"concurrent={self._open_trade_count}/{self.current_concurrent_limit}")
        return rec

    def register_close(self, rec: TradeRecord, exit_price: float, pnl: float) -> None:
        rec.exit_price         = exit_price
        rec.pnl                = pnl
        rec.won                = pnl > 0
        self._open_trade_count = max(0, self._open_trade_count - 1)
        self.total_pnl        += pnl
        self.record_result(rec.won)
        logger.info(
            f"Trade CLOSE | {rec.symbol} {rec.direction} | pnl=${pnl:+.4f} | "
            f"{'WIN' if rec.won else 'LOSS'} | streak={self._current_streak} | "
            f"balance=${self._current_balance:.4f} | open={self._open_trade_count}")

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

    def summary(self) -> dict:
        total = self.wins + self.losses
        return {
            "current_balance":   round(self._current_balance, 4),
            "day_start_balance": round(self._day_start_balance, 4),
            "daily_pnl":         round(self.daily_pnl, 4),
            "daily_pnl_pct":     round(self.daily_pnl_pct * 100, 2),
            "total_trades":      self.total_trades,
            "wins":              self.wins,
            "losses":            self.losses,
            "win_rate":          round(self.wins / total * 100, 1) if total else 0.0,
            "total_pnl":         round(self.total_pnl, 4),
            "open_trades":       self._open_trade_count,
            "streak":            self._current_streak,
            "win_streak":        self.win_streak,
            "loss_streak":       self.loss_streak,
            "multiplier":        self.current_multiplier,
            "concurrent_limit":  self.current_concurrent_limit,
            "bot_state":         self._bot_state.name,
        }

"""
risk_manager.py – Phase C risk overlay for the SIFM bot.

v17 — PLS (Progressive Loss Scaling) rewrite.

KEY CHANGES vs v16
──────────────────
• Replaced all aggressive/Martingale-adjacent scaling with the
  research-documented safest recovery method: Progressive Loss Scaling.
  PLS scales stake and concurrency UP only on confirmed win streaks,
  and collapses INSTANTLY to baseline on any loss. Martingale-style
  doubling-after-loss is never used anywhere in this module.

BASE STAKE
──────────
  base_stake = config.BASE_STAKE_PCT × current_live_balance
  Recalculated from live balance before EVERY single trade.
  As balance grows, base stake grows — pure compounding.
  Fixed dollar amounts are never used as base.

PLS WIN SCALING
───────────────
  Streak 0–2   →  1.0×  stake
  Streak ≥ 3   →  1.5×  stake
  Streak ≥ 5   →  2.0×  stake
  Streak ≥ 8   →  3.0×  stake
  Streak ≥ 12  →  4.0×  stake

  Final stake = base × multiplier, hard-capped at config.MAX_STAKE.

ON ANY LOSS — IMMEDIATE RESET
──────────────────────────────
  _win_streak  → 0
  _multiplier  → 1.0
  _extra_slots → 0
  Next base recalculated immediately from current (reduced) balance.
  Stake is NEVER increased to recover a loss.

CONCURRENT SLOT SCALING
────────────────────────
  Default: config.MAX_CONCURRENT_TRADES
  Streak ≥ 3   →  +2 extra slots
  Streak ≥ 5   →  +4 extra slots
  Streak ≥ 8   →  +6 extra slots
  Streak ≥ 12  →  +8 extra slots
  Any loss     →  reset to default immediately

can_trade() GATES
──────────────────
  True iff: open_contracts < current_concurrent_limit
        AND current_balance > MIN_STAKE
  Nothing else blocks trading.

POST-CLOSE LOG
───────────────
  PLS | Streak: +{N} | Multiplier: {M}x | Balance: ${B:.4f} | Next stake: ${S:.4f}
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


# ── PLS tier table ──────────────────────────────────────────────────────────
#
# Disabled per user request: stake no longer scales with win streak.
# _pls_tier() always returns (1.0, 0) — kept as a function (rather than
# deleted) so _update_multiplier()/callers don't need to change.
_PLS_TIERS: list[tuple[int, float, int]] = [
    (0, 1.0, 0),   # baseline — always matches, streak has no effect
]


def _pls_tier(win_streak: int) -> tuple[float, int]:
    """
    Win-streak stake scaling is disabled — always returns (1.0, 0)
    regardless of win_streak. Stake sizing instead comes purely from
    base_stake = BASE_STAKE_PCT × current_balance in _compute_stake().
    """
    return 1.0, 0


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


# ── RiskManager ───────────────────────────────────────────────────────────────

class RiskManager:
    """
    Stateful risk and position-sizing layer using Progressive Loss Scaling.

    PLS Design
    ──────────
    base_stake   = BASE_STAKE_PCT × live_balance  (recalculated every trade)
    actual_stake = base_stake × pls_multiplier    (streak-driven, win-only)
    actual_stake clamped to [MIN_STAKE, MAX_STAKE]

    Any loss instantly collapses: multiplier → 1.0×, extra_slots → 0,
    win_streak → 0. Stake NEVER increases to recover a loss. This is
    explicitly not Martingale — there is no doubling after a loss, and
    scaling only ever happens off confirmed, already-banked wins.
    """

    def __init__(
        self,
        risk_per_trade: float = None,
        min_stake:      float = None,
        max_stake:      float = None,
        max_concurrent: int   = None,
        deriv_client=None,
    ):
        # Config resolution
        self.base_stake_pct = getattr(config, "BASE_STAKE_PCT", 0.01)
        self.risk_per_trade = risk_per_trade or self.base_stake_pct
        self.min_stake      = min_stake      or getattr(config, "MIN_STAKE",             0.35)
        self.max_stake      = max_stake      or getattr(config, "MAX_STAKE",             50.0)
        self.max_concurrent = max_concurrent or getattr(config, "MAX_CONCURRENT_TRADES", 15)
        self._deriv_client  = deriv_client

        # Live balance — updated by set_balance() and _fetch_live_balance()
        self._current_balance: float = 0.0
        self._balance_cycle:   int   = -1
        self._current_cycle:   int   = 0

        # Day-reset bookkeeping
        self._day_tag:           str   = ""
        self._day_start_balance: float = 0.0

        # PLS win-streak counter (never goes negative; loss resets to 0)
        self._win_streak: int = 0

        # Explicit PLS state mirrors (used for immediate reset on loss)
        self._multiplier:  float = 1.0
        self._extra_slots: int   = 0

        # Concurrent trade tracking
        self._open_trade_count: int = 0

        # Bot state
        self._bot_state: BotState = BotState.RUNNING

        # Pause-cycle countdown (kept for bot_engine compatibility)
        self._pause_cycles_remaining: int = 0

        # Session stats
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

    # ── Cycle clock ───────────────────────────────────────────────────────────

    def tick_cycle(self) -> None:
        self._current_cycle += 1

    # ── Pause-cycle management (bot_engine compatibility) ─────────────────────

    @property
    def pause_cycles_remaining(self) -> int:
        return self._pause_cycles_remaining

    def decrement_pause(self) -> None:
        if self._pause_cycles_remaining > 0:
            self._pause_cycles_remaining -= 1

    def consume_pause_cycle(self) -> None:
        self.decrement_pause()

    # ── Balance management ────────────────────────────────────────────────────

    def set_balance(self, balance: float) -> None:
        self._current_balance = balance
        if self._day_start_balance == 0:
            self._day_start_balance = balance
        logger.info(f"BALANCE UPDATED: ${balance:.4f}")
        self._balance_cycle   = self._current_cycle
        self._handle_day_rollover(balance)

    async def _fetch_live_balance(self) -> float:
        """
        Return the most current balance available.
        Hits the Deriv client when stale; falls back to cached value if
        the client is absent or the call fails.
        """
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
        today = _today_tag()
        if today != self._day_tag:
            self._day_tag        = today
            self._win_streak     = 0
            self._multiplier     = 1.0
            self._extra_slots    = 0
            self._day_start_balance = balance if balance > 0 else self._day_start_balance
            logger.info(
                f"New trading day {today} | "
                f"Starting balance: ${self._day_start_balance:.4f}")
        if self._day_start_balance == 0.0 and balance > 0:
            self._day_start_balance = balance
            logger.info(f"Day-start balance initialised: ${balance:.4f}")

    # ── PLS multiplier update ──────────────────────────────────────────────────

    def _update_multiplier(self) -> None:
        """Sync _multiplier and _extra_slots to the current win streak tier."""
        self._multiplier, self._extra_slots = _pls_tier(self._win_streak)

    # ── PLS properties (spec-required public interface) ────────────────────────

    @property
    def current_streak(self) -> int:
        """Current consecutive win count (0 after any loss)."""
        return self._win_streak

    @property
    def current_multiplier(self) -> float:
        """Active PLS stake multiplier derived from win streak."""
        return self._multiplier

    @property
    def current_concurrent_limit(self) -> int:
        """
        Effective concurrent-trade ceiling.
        = MAX_CONCURRENT_TRADES + win-streak slot bonus.
        Collapses to MAX_CONCURRENT_TRADES on any loss.
        """
        return self.max_concurrent + self._extra_slots

    @property
    def next_stake(self) -> float:
        """
        Preview of the stake that would be used for the very next trade,
        computed from the current cached balance (synchronous; no await).
        """
        return self._compute_stake(self._current_balance)

    # ── Legacy aliases (bot_engine compatibility) ──────────────────────────────

    @property
    def win_streak(self) -> int:
        return self._win_streak

    @property
    def loss_streak(self) -> int:
        # PLS tracks only win streaks; loss streak is always 0 or 1
        return 0

    # ── PLS stake computation ───────────────────────────────────────────────────

    def _compute_stake(self, balance: float) -> float:
        """
        Core PLS formula (synchronous, no I/O).

          base_stake   = max(BASE_STAKE_PCT × balance, MIN_STAKE)
          actual_stake = base_stake × current_multiplier
          actual_stake = clamp(actual_stake, MIN_STAKE, MAX_STAKE)
        """
        if balance <= 0:
            return self.min_stake

        base_stake   = max(balance * self.base_stake_pct, self.min_stake)
        actual_stake = base_stake * self._multiplier
        actual_stake = min(actual_stake, self.max_stake)
        actual_stake = max(actual_stake, self.min_stake)
        return round(actual_stake, 2)

    async def calculate_stake(self) -> float:
        """
        Fetch live balance, apply the PLS formula, log, and return
        the stake.

        Log format:
          STAKE: $X (base=$Y ×M streak=+Z)
        """
        balance = await self._fetch_live_balance()

        base  = max(self._current_balance * self.base_stake_pct, self.min_stake)
        stake = round(base * self._multiplier, 2)
        stake = min(stake, self.max_stake)
        stake = max(stake, self.min_stake)

        logger.info(
            f"STAKE: ${stake:.4f} "
            f"(base=${base:.4f} "
            f"×{self._multiplier:.1f} "
            f"streak=+{self._win_streak})")

        return stake

    # ── PLS can_trade gate ──────────────────────────────────────────────────────

    def can_trade(self) -> bool:
        """
        Return True iff ALL of:
          1. open_contracts < current_concurrent_limit
          2. current_balance > MIN_STAKE

        Nothing else blocks trading.
        Symbol suspension is handled by symbol_manager, not here.
        """
        if self._current_balance <= self.min_stake:
            logger.debug(
                f"can_trade: balance ${self._current_balance:.4f} "
                f"<= MIN_STAKE ${self.min_stake:.2f}")
            return False

        if self._open_trade_count >= self.current_concurrent_limit:
            logger.debug(
                f"can_trade: concurrent limit reached "
                f"({self._open_trade_count}/{self.current_concurrent_limit})")
            return False

        return True

    # ── PLS result recording ────────────────────────────────────────────────────

    def record_result(self, won: bool) -> None:
        """
        Update the PLS win streak and emit the mandated post-trade log line.

        WIN  → increment streak → _update_multiplier() raises tier.
        LOSS → immediate reset: win_streak → 0, multiplier → 1.0,
               extra_slots → 0. Stake is NEVER raised to recover a loss —
               this is the core distinction from Martingale.

        Log format (win):
          PLS | Streak: +{N} | Multiplier: {M}x | Balance: ${B:.4f} | Next stake: ${S:.4f}
        Log format (loss):
          PLS | Streak: +0 (reset) | Multiplier: 1.0x | Balance: ${B:.4f} | Next stake: ${S:.4f}
        """
        if won:
            self.wins        += 1
            self._win_streak += 1
            self._update_multiplier()
        else:
            self.losses          += 1
            self._win_streak      = 0
            self._multiplier      = 1.0
            self._extra_slots     = 0

        balance = self._current_balance if self._current_balance > 0 else self.min_stake
        ns      = self._compute_stake(balance)

        streak_label = (
            f"+{self._win_streak}" if won
            else "+0 (reset)"
        )

        logger.info(
            f"PLS | Streak: {streak_label} | "
            f"Multiplier: {self._multiplier:.1f}x | "
            f"Balance: ${balance:.4f} | "
            f"Next stake: ${ns:.4f}")

    # ── Trade lifecycle ─────────────────────────────────────────────────────────

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
            f"streak=+{self._win_streak} | "
            f"multiplier={self._multiplier:.1f}x | "
            f"concurrent={self._open_trade_count}/{self.current_concurrent_limit}")
        return rec

    def register_close(
        self,
        rec:        TradeRecord,
        exit_price: float,
        pnl:        float,
    ) -> None:
        rec.exit_price         = exit_price
        rec.pnl                = pnl
        rec.won                = pnl > 0
        self._open_trade_count = max(0, self._open_trade_count - 1)
        self.total_pnl        += pnl

        self.record_result(rec.won)

        logger.info(
            f"Trade CLOSE | {rec.symbol} {rec.direction} | "
            f"pnl=${pnl:+.4f} | {'WIN' if rec.won else 'LOSS'} | "
            f"streak=+{self._win_streak} | "
            f"balance=${self._current_balance:.4f} | "
            f"open={self._open_trade_count}")

    # ── Session helpers ─────────────────────────────────────────────────────────

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
            "current_balance":    round(self._current_balance, 4),
            "day_start_balance":  round(self._day_start_balance, 4),
            "daily_pnl":          round(self.daily_pnl, 4),
            "daily_pnl_pct":      round(self.daily_pnl_pct * 100, 2),
            "total_trades":       self.total_trades,
            "wins":               self.wins,
            "losses":             self.losses,
            "win_rate":           round(self.wins / total * 100, 1) if total else 0.0,
            "total_pnl":          round(self.total_pnl, 4),
            "open_trades":        self._open_trade_count,
            # PLS state
            "current_streak":     self.current_streak,
            "current_multiplier": self.current_multiplier,
            "next_stake":         self.next_stake,
            "concurrent_limit":   self.current_concurrent_limit,
            "bot_state":          self._bot_state.name,
        }


# ── Module-level helpers ──────────────────────────────────────────────────────

def _today_tag() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

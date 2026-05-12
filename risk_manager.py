"""
risk_manager.py – Phase C risk overlay for the SIFM bot.

Responsibilities:
  • Track daily starting balance and current balance
  • Enforce the 9% daily loss limit (pause until UTC midnight)
  • Compound position sizing (1% of CURRENT balance per trade)
  • Apply minimum / maximum stake constraints
  • Record trade outcomes
"""

import time
import logging
import datetime
from dataclasses import dataclass, field
from typing import Optional
import config

logger = logging.getLogger(__name__)


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


class RiskManager:
    """
    Manages risk rules for a single trading session.

    The '9% daily loss' rule:
      At the start of each UTC day the starting balance is snapshotted.
      If the CURRENT balance ever drops below (day_start × 0.91),
      trading is paused for the remainder of that UTC day.
    """

    def __init__(self,
                 daily_loss_limit: float = 0.09,
                 risk_per_trade:   float = 0.01,
                 min_stake:        float = 0.35,
                 max_stake:        float = 500.0,
                 max_concurrent:   int   = 3):

        self.daily_loss_limit = daily_loss_limit
        self.risk_per_trade   = risk_per_trade
        self.min_stake        = min_stake
        self.max_stake        = max_stake
        self.max_concurrent   = max_concurrent

        self._current_balance: float = 0.0
        self._day_start_balance: float = 0.0
        self._day_tag: str = ""        # "YYYY-MM-DD" of the current day
        self._paused:  bool = False
        self._open_trade_count: int = 0
        self._trades: list = []

        # Stats
        self.total_trades: int = 0
        self.wins:         int = 0
        self.losses:       int = 0
        self.total_pnl:    float = 0.0

    # ── Balance management ────────────────────────────────────────────────────

    def set_balance(self, balance: float):
        """Called whenever a fresh balance is received from Deriv."""
        today = self._today_tag()

        if today != self._day_tag:
            # New UTC day → reset daily tracking
            self._day_tag          = today
            self._day_start_balance = balance
            self._paused           = False
            logger.info(f"New trading day {today} | "
                        f"Starting balance: ${balance:.4f}")

        self._current_balance = balance
        self._check_loss_limit()

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

    # ── Loss limit check ──────────────────────────────────────────────────────

    def _check_loss_limit(self):
        if self._day_start_balance == 0:
            return
        loss_pct = -self.daily_pnl_pct          # positive when we're losing
        if loss_pct >= self.daily_loss_limit:
            if not self._paused:
                self._paused = True
                logger.warning(
                    f"⛔ Daily loss limit reached! "
                    f"Down {loss_pct*100:.2f}% from today's start "
                    f"(${self._day_start_balance:.4f} → ${self._current_balance:.4f}). "
                    f"Trading PAUSED until UTC midnight.")
        else:
            if self._paused and loss_pct < self.daily_loss_limit * 0.8:
                # small buffer before re-enabling (won't happen same day
                # unless balance recovers via external deposit)
                pass  # keep paused until midnight
import os

if os.environ.get("FORCE_RESET_DAILY_LOSS", "").lower() == "true":
    risk_manager.force_reset_daily(current_balance)

    @property
    def is_paused(self) -> bool:
        """True if trading is paused for today due to loss limit or max concurrent."""
        return self._paused

    def can_trade(self) -> bool:
        """Returns True if a new trade is allowed right now."""
        if self._paused:
            return False
        if self._current_balance <= 0:
            return False
        if self._open_trade_count >= self.max_concurrent:
            logger.debug(f"Max concurrent trades reached ({self.max_concurrent})")
            return False
        return True

    # ── Position sizing ───────────────────────────────────────────────────────

    def calculate_stake(self) -> float:
        """
        Compound position size = 1% of CURRENT balance.
        Always between [min_stake, max_stake].
        """
        raw = self._current_balance * self.risk_per_trade
        stake = max(self.min_stake, min(raw, self.max_stake))
        return round(stake, 2)

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    def register_open(self, symbol: str, direction: str,
                      stake: float, entry_price: float) -> TradeRecord:
        rec = TradeRecord(symbol=symbol, direction=direction,
                          stake=stake, entry_price=entry_price)
        self._trades.append(rec)
        self._open_trade_count += 1
        self.total_trades += 1
        logger.info(f"Trade OPEN  | {symbol} {direction} | "
                    f"stake=${stake:.2f} | price={entry_price}")
        return rec

    def register_close(self, rec: TradeRecord, exit_price: float, pnl: float):
        rec.exit_price = exit_price
        rec.pnl        = pnl
        rec.won        = pnl > 0
        self._open_trade_count = max(0, self._open_trade_count - 1)
        self.total_pnl += pnl
        if rec.won:
            self.wins += 1
        else:
            self.losses += 1
        logger.info(f"Trade CLOSE | {rec.symbol} {rec.direction} | "
                    f"pnl=${pnl:+.4f} | {'WIN' if rec.won else 'LOSS'} | "
                    f"balance=${self._current_balance:.4f}")

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _today_tag() -> str:
        return datetime.datetime.utcnow().strftime("%Y-%m-%d")

    def minutes_until_midnight(self) -> float:
        now = datetime.datetime.utcnow()
        midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return (midnight - now).total_seconds() / 60

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
            "win_rate":          round(self.wins / total * 100, 1) if total else 0,
            "total_pnl":         round(self.total_pnl, 4),
            "paused":            self._paused,
            "open_trades":       self._open_trade_count,
        }

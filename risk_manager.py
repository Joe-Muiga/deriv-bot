"""
risk_manager.py – Phase C risk overlay for the SIFM bot.

Changes vs previous version:
  • Tracks current win/loss streak internally (_current_streak).
  • calculate_stake() uses the streak to scale position size:
      - Win streak  → stake grows by WIN_STREAK_STAKE_FACTOR per win, capped.
      - Loss streak → stake collapses to MIN_STAKE immediately.
  • Trading pauses ONLY when balance drops 90% below the day-start value.
    No other balance-based throttle exists.
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

    def __init__(self,
                 daily_loss_limit: float = 0.90,
                 risk_per_trade:   float = 0.01,
                 min_stake:        float = 0.35,
                 max_stake:        float = 500.0,
                 max_concurrent:   int   = 10):

        self.daily_loss_limit = daily_loss_limit
        self.risk_per_trade   = risk_per_trade
        self.min_stake        = min_stake
        self.max_stake        = max_stake
        self.max_concurrent   = max_concurrent

        self._current_balance:   float = 0.0
        self._day_start_balance: float = 0.0
        self._day_tag:           str   = ""
        self._paused:            bool  = False
        self._open_trade_count:  int   = 0
        self._trades:            list  = []

        # Streak tracking: positive = consecutive wins, negative = consecutive losses
        self._current_streak: int = 0

        # Session stats
        self.total_trades: int   = 0
        self.wins:         int   = 0
        self.losses:       int   = 0
        self.total_pnl:    float = 0.0

    # ── Balance management ────────────────────────────────────────────────────

    def set_balance(self, balance: float):
        today = self._today_tag()
        if today != self._day_tag:
            self._day_tag           = today
            self._day_start_balance = balance
            self._paused            = False
            self._current_streak    = 0
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

    @property
    def current_streak(self) -> int:
        """Positive = win streak length, negative = loss streak length."""
        return self._current_streak

    # ── Loss limit check ──────────────────────────────────────────────────────

    def _check_loss_limit(self):
        if self._day_start_balance == 0:
            return
        loss_pct = -self.daily_pnl_pct
        if loss_pct >= self.daily_loss_limit and not self._paused:
            self._paused = True
            logger.warning(
                f"⛔ 90% daily loss limit reached! "
                f"Down {loss_pct*100:.2f}% "
                f"(${self._day_start_balance:.4f} → ${self._current_balance:.4f}). "
                f"Trading PAUSED until UTC midnight.")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def can_trade(self) -> bool:
        if self._paused:
            return False
        if self._current_balance <= 0:
            return False
        if self._open_trade_count >= self.max_concurrent:
            logger.debug(f"Max concurrent trades reached ({self.max_concurrent})")
            return False
        return True

    # ── Position sizing (streak-aware) ────────────────────────────────────────

    def calculate_stake(self) -> float:
        """
        Returns the stake for the next trade based on the current streak.

        Win streak:  stake = base × (1 + streak × factor), capped at 3× base.
        Loss streak: stake = MIN_STAKE (no risk until streak resets).
        Neutral (0): stake = base (normal 1% risk).
        """
        base = self._current_balance * self.risk_per_trade

        if self._current_streak > 0:
            multiplier = min(
                1.0 + self._current_streak * config.WIN_STREAK_STAKE_FACTOR,
                config.MAX_WIN_STREAK_MULT)
            raw = base * multiplier
            logger.debug(f"Win streak {self._current_streak} → "
                         f"stake multiplier {multiplier:.2f}x")
        elif self._current_streak < 0:
            # Loss streak: always use minimum stake
            raw = self.min_stake
            logger.debug(f"Loss streak {self._current_streak} → "
                         f"using minimum stake ${self.min_stake:.2f}")
        else:
            raw = base

        return round(max(self.min_stake, min(raw, self.max_stake)), 2)

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    def register_open(self, symbol: str, direction: str,
                      stake: float, entry_price: float) -> TradeRecord:
        rec = TradeRecord(symbol=symbol, direction=direction,
                          stake=stake, entry_price=entry_price)
        self._trades.append(rec)
        self._open_trade_count += 1
        self.total_trades      += 1
        logger.info(f"Trade OPEN  | {symbol} {direction} | "
                    f"stake=${stake:.2f} | price={entry_price} | "
                    f"streak={self._current_streak}")
        return rec

    def register_close(self, rec: TradeRecord, exit_price: float, pnl: float):
        rec.exit_price         = exit_price
        rec.pnl                = pnl
        rec.won                = pnl > 0
        self._open_trade_count = max(0, self._open_trade_count - 1)
        self.total_pnl        += pnl

        if rec.won:
            self.wins           += 1
            # Extend win streak, reset loss streak
            self._current_streak = max(0, self._current_streak) + 1
        else:
            self.losses         += 1
            # Extend loss streak (negative), reset win streak
            self._current_streak = min(0, self._current_streak) - 1

        logger.info(f"Trade CLOSE | {rec.symbol} {rec.direction} | "
                    f"pnl=${pnl:+.4f} | {'WIN' if rec.won else 'LOSS'} | "
                    f"streak={self._current_streak} | "
                    f"balance=${self._current_balance:.4f}")

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _today_tag() -> str:
        return datetime.datetime.utcnow().strftime("%Y-%m-%d")

    def minutes_until_midnight(self) -> float:
        now      = datetime.datetime.utcnow()
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
            "streak":            self._current_streak,
        }

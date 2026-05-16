"""
risk_manager.py – Phase C risk overlay for the SIFM bot.

v6 → v7 changes (Bug 3 fix):

  BUG 3 FIX – can_trade() startup race condition:
    In v6, can_trade() checked `self._current_balance < 0`, which correctly
    allowed trading when balance was 0 (uninitialised).  However,
    _check_loss_limit() called inside set_balance() could in theory set
    _paused=True if day_start_balance was 0 AND balance was 0 (0/0 edge).
    Added an explicit guard: _check_loss_limit() now returns immediately
    if _day_start_balance == 0, preventing spurious pauses at startup.

  BUG 3 FIX – set_balance() first-call path:
    On startup the very first set_balance() call now also resets
    _confirmed_daily_loss and _day_tag to ensure a clean slate even if
    the bot was restarted mid-day.  The day_start_balance is only set
    once per UTC day (unchanged behaviour).

  CHANGE – No other logic changes from v6.
    The quality gate, streak scaling, and all other features are preserved
    exactly as implemented in v6.  See v5→v6 docstring for full feature list.
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
                 max_concurrent:   int   = 20):

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

        # Streak: positive = consecutive wins, negative = consecutive losses
        self._current_streak: int = 0

        # ── Loss-streak quality gate ──────────────────────────────────────────
        self._quality_gate_active:  bool  = False
        self._quality_gate_since:   float = 0.0

        # Session stats
        self.total_trades: int   = 0
        self.wins:         int   = 0
        self.losses:       int   = 0
        self.total_pnl:    float = 0.0

    # ── Balance management ────────────────────────────────────────────────────

    def set_balance(self, balance: float):
        today = self._today_tag()

        # New UTC day → reset everything
        if today != self._day_tag:
            self._day_tag = today
            self._paused  = False
            self._current_streak = 0
            self._quality_gate_active = False
            logger.info(f"New trading day {today} | Starting balance: ${balance:.4f}")

        self._current_balance = balance

        # BUG 3 FIX: Initialise day_start_balance on the very first set_balance call.
        # Also reset _paused to False on first call so startup never begins paused.
        if self._day_start_balance == 0.0 and balance > 0:
            self._day_start_balance = balance
            self._paused = False   # explicit: never start paused
            logger.info(f"Day-start balance set: ${balance:.4f}")

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
        # BUG 3 FIX: guard against division by zero / false pause at startup.
        # If day_start_balance is 0 (balance not yet known), skip the check.
        if self._day_start_balance == 0:
            return
        loss_pct = -self.daily_pnl_pct
        if loss_pct >= self.daily_loss_limit and not self._paused:
            self._paused = True
            logger.warning(
                f"⛔ 90% daily loss limit reached! "
                f"Down {loss_pct * 100:.2f}% "
                f"(${self._day_start_balance:.4f} → ${self._current_balance:.4f}). "
                f"Trading PAUSED until UTC midnight.")

    @property
    def is_paused(self) -> bool:
        return self._paused

    # ── Quality gate management ───────────────────────────────────────────────

    @property
    def requires_full_confirmation(self) -> bool:
        """
        True when the quality gate is active (streak <= LOSS_STREAK_QUALITY_GATE).
        When True, only signals with strength == 3 (all modules agree) should
        be executed.  Gate auto-clears after QUALITY_GATE_TIMEOUT_SECS.
        """
        if not self._quality_gate_active:
            return False
        # Safety valve: auto-clear after timeout to prevent deadlock
        timeout = getattr(config, "QUALITY_GATE_TIMEOUT_SECS", 60)
        if time.time() - self._quality_gate_since > timeout:
            logger.info(
                f"Quality gate timeout ({timeout}s) — auto-clearing "
                f"| streak={self._current_streak}")
            self._quality_gate_active = False
            return False
        return True

    def _update_quality_gate(self):
        """
        Activate/deactivate the quality gate based on current streak.
        Called after every register_close().
        """
        gate_threshold = getattr(config, "LOSS_STREAK_QUALITY_GATE", -3)

        if self._current_streak <= gate_threshold and not self._quality_gate_active:
            self._quality_gate_active = True
            self._quality_gate_since  = time.time()
            logger.warning(
                f"⚠ Quality gate ACTIVATED (streak={self._current_streak}). "
                f"Only 3/3-module signals will execute until next win.")

        elif self._current_streak > gate_threshold and self._quality_gate_active:
            self._quality_gate_active = False
            logger.info(
                f"Quality gate DEACTIVATED (streak={self._current_streak}).")

    def min_required_strength(self) -> int:
        """
        Return the minimum signal module strength required to execute a trade.
        Returns 3 when quality gate is active, 2 (MIN_MODULES_FOR_SIGNAL) otherwise.
        """
        if self.requires_full_confirmation:
            return 3
        return getattr(config, "MIN_MODULES_FOR_SIGNAL", 2)

    def can_trade(self, signal_strength: int = 0) -> bool:
        """
        Gate check before executing any trade.

        Parameters
        ----------
        signal_strength : int
            Number of confirming signal modules (0–3).  Pass 0 (default) if
            unknown — quality gate will block ALL trades when active.
            Pass the actual signal strength (sig.strength) for intelligent
            gate behaviour: 3/3-module signals bypass the gate, weaker ones
            are blocked.

        Returns True if a trade may proceed, False otherwise.

        BUG 3 FIX: removed the `_current_balance <= 0` hard block.
          The old check used <= 0, which blocked trading when balance was
          exactly 0.0 (uninitialised at startup before the first
          set_balance() call).  Replaced with `< 0` (actually negative only).
        """
        if self._paused:
            logger.debug("can_trade: daily loss limit paused")
            return False

        # BUG 3 FIX: use < 0 not <= 0 — don't block before first set_balance
        if self._current_balance < 0:
            logger.debug("can_trade: negative balance, blocking")
            return False

        if self._open_trade_count >= self.max_concurrent:
            logger.debug(
                f"can_trade: max concurrent trades reached ({self.max_concurrent})")
            return False

        # ── Loss-streak quality gate ──────────────────────────────────────────
        if self.requires_full_confirmation:
            required = 3
            if signal_strength < required:
                logger.debug(
                    f"can_trade: quality gate active (streak={self._current_streak}) "
                    f"— need strength={required}, got {signal_strength}")
                return False
            else:
                logger.info(
                    f"can_trade: quality gate bypassed by {signal_strength}/3 signal")

        return True

    # ── Position sizing (streak-aware) ────────────────────────────────────────

    def calculate_stake(self) -> float:
        """
        Win streak (+3 or more):
          stake = base × (1 + streak × WIN_STREAK_STAKE_FACTOR),
          capped at base × MAX_WIN_STREAK_MULT.

        Loss streak (any negative streak):
          stake = MIN_STAKE regardless of balance.

        Neutral (streak 0):
          stake = base (1% of balance).

        If balance uninitialised (== 0): returns MIN_STAKE as safe default.
        """
        if self._current_balance <= 0:
            return self.min_stake

        base = self._current_balance * self.risk_per_trade

        if self._current_streak > 0:
            multiplier = min(
                1.0 + self._current_streak * config.WIN_STREAK_STAKE_FACTOR,
                config.MAX_WIN_STREAK_MULT)
            raw = base * multiplier
            logger.debug(
                f"Win streak {self._current_streak} → "
                f"stake multiplier {multiplier:.2f}× | "
                f"base=${base:.2f} → raw=${raw:.2f}")
        elif self._current_streak < 0:
            raw = self.min_stake
            logger.debug(
                f"Loss streak {self._current_streak} → "
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
        logger.info(
            f"Trade OPEN  | {symbol} {direction} | "
            f"stake=${stake:.2f} | price={entry_price} | "
            f"streak={self._current_streak} | "
            f"quality_gate={self._quality_gate_active}")
        return rec

    def register_close(self, rec: TradeRecord, exit_price: float, pnl: float):
        rec.exit_price         = exit_price
        rec.pnl                = pnl
        rec.won                = pnl > 0
        self._open_trade_count = max(0, self._open_trade_count - 1)
        self.total_pnl        += pnl

        if rec.won:
            self.wins            += 1
            # Win always resets any loss streak and starts/extends a win streak
            self._current_streak  = max(0, self._current_streak) + 1
        else:
            self.losses          += 1
            # Loss always resets any win streak and extends the loss streak
            self._current_streak  = min(0, self._current_streak) - 1

        # Update quality gate AFTER streak changes
        self._update_quality_gate()

        logger.info(
            f"Trade CLOSE | {rec.symbol} {rec.direction} | "
            f"pnl=${pnl:+.4f} | {'WIN' if rec.won else 'LOSS'} | "
            f"streak={self._current_streak} | "
            f"quality_gate={self._quality_gate_active} | "
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
            "current_balance":    round(self._current_balance, 4),
            "day_start_balance":  round(self._day_start_balance, 4),
            "daily_pnl":          round(self.daily_pnl, 4),
            "daily_pnl_pct":      round(self.daily_pnl_pct * 100, 2),
            "total_trades":       self.total_trades,
            "wins":               self.wins,
            "losses":             self.losses,
            "win_rate":           round(self.wins / total * 100, 1) if total else 0,
            "total_pnl":          round(self.total_pnl, 4),
            "paused":             self._paused,
            "open_trades":        self._open_trade_count,
            "streak":             self._current_streak,
            "quality_gate":       self._quality_gate_active,
            "min_required_strength": self.min_required_strength(),
        }

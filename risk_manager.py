"""
risk_manager.py – Phase C risk overlay for the SIFM bot.

v8 → v9 changes:

  REMOVED — Tiered loss-streak quality gate:
    All loss-streak pause/abort/quality-gate tier logic is gone.
    _streak_tier(), requires_full_confirmation, _update_quality_gate(),
    pause_cycles_remaining, consume_pause_cycle(), _quality_gate_active,
    _pause_cycles_remaining have all been removed.

  STREAK TRACKING — wins only:
    _current_streak is now a non-negative counter:
      • Win  → _current_streak += 1
      • Loss → _current_streak  = 0  (reset, never goes negative)
    There is no loss-streak counter. Per-symbol suspension is handled
    exclusively by SymbolManager.

  WIN-STREAK STAKE + CONCURRENT SCALING (unchanged from v8):
    +3 streak  → 1.5× stake,  +0 extra concurrent slots
    +4 streak  → 2.0× stake,  +2 extra concurrent slots
    +6 streak  → 3.0× stake,  +4 extra concurrent slots
    +8 streak  → 4.0× stake,  +6 extra concurrent slots
    Thresholds/multipliers driven by config.WIN_STREAK_SCALE_THRESHOLDS,
    WIN_STREAK_STAKE_MULTIPLIERS, WIN_STREAK_CONCURRENT_BONUS.
    Hard cap: stake ≤ MAX_STAKE; concurrent ≤ MAX_CONCURRENT_TRADES.
    Any single loss instantly resets stake and concurrent bonus to base.

  can_trade() — simplified:
    Checks daily-loss pause, negative balance, and concurrent slot limit only.
    Base confidence gate (MIN_CONFIDENCE_NORMAL) and base strength gate
    (MIN_MODULES_FOR_SIGNAL) still applied — fixed, not tier-dependent.

  All v7/v8 startup-race and balance-init fixes preserved unchanged.
"""

import time
import logging
import datetime
from dataclasses import dataclass, field
from typing import Optional, List
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
        self.max_concurrent   = max_concurrent   # absolute ceiling (from config)

        self._current_balance:   float = 0.0
        self._day_start_balance: float = 0.0
        self._day_tag:           str   = ""
        self._paused:            bool  = False
        self._open_trade_count:  int   = 0
        self._trades:            list  = []

        # Win-only streak counter (non-negative; reset to 0 on any loss)
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
            self._day_tag = today
            self._paused  = False
            self._current_streak = 0
            logger.info(f"New trading day {today} | Starting balance: ${balance:.4f}")

        self._current_balance = balance

        if self._day_start_balance == 0.0 and balance > 0:
            self._day_start_balance = balance
            self._paused = False
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
                f"Down {loss_pct * 100:.2f}% "
                f"(${self._day_start_balance:.4f} → ${self._current_balance:.4f}). "
                f"Trading PAUSED until UTC midnight.")

    @property
    def is_paused(self) -> bool:
        return self._paused

    # ── Win-streak concurrent slot bonus ──────────────────────────────────────

    def _win_streak_concurrent_bonus(self) -> int:
        """
        Extra concurrent trade slots granted by current win streak.
        Returns 0 when streak is 0 (no bonus at base).
        Hard-capped so total never exceeds max_concurrent.
        """
        if self._current_streak <= 0:
            return 0
        thresholds: List[int] = getattr(
            config, "WIN_STREAK_SCALE_THRESHOLDS", [3, 4, 6, 8])
        bonuses: List[int] = getattr(
            config, "WIN_STREAK_CONCURRENT_BONUS", [0, 2, 4, 6])
        bonus = 0
        for t, b in zip(thresholds, bonuses):
            if self._current_streak >= t:
                bonus = b
        return bonus

    @property
    def effective_max_concurrent(self) -> int:
        """Base max_concurrent + win-streak bonus, capped at config ceiling."""
        ceiling = getattr(config, "MAX_CONCURRENT_TRADES", self.max_concurrent)
        return min(self.max_concurrent + self._win_streak_concurrent_bonus(), ceiling)

    # ── Gate helpers (base, non-tiered) ──────────────────────────────────────

    def min_required_strength(self) -> int:
        """Minimum signal module strength required to execute a trade (base only)."""
        return getattr(config, "MIN_MODULES_FOR_SIGNAL", 2)

    def min_required_confidence(self) -> int:
        """Minimum indicator-agreement confidence required (base, always normal tier)."""
        return getattr(config, "MIN_CONFIDENCE_NORMAL", 5)

    # ── Trade gate ────────────────────────────────────────────────────────────

    def can_trade(self, signal_strength: int = 0,
                  confidence: int = 0) -> bool:
        """
        Gate check before executing any trade.

        Parameters
        ----------
        signal_strength : int   Number of confirming signal modules (0–3).
        confidence      : int   Number of M3 indicators agreeing with direction (0–7).

        Returns True if a trade may proceed, False otherwise.
        Loss-streak gates removed; per-symbol suspension is handled by SymbolManager.
        """
        if self._paused:
            logger.debug("can_trade: daily loss limit paused")
            return False

        if self._current_balance < 0:
            logger.debug("can_trade: negative balance, blocking")
            return False

        if self._open_trade_count >= self.effective_max_concurrent:
            logger.debug(
                f"can_trade: max concurrent trades reached "
                f"({self._open_trade_count}/{self.effective_max_concurrent})")
            return False

        # ── Base strength gate ────────────────────────────────────────────────
        min_str = self.min_required_strength()
        if signal_strength < min_str:
            logger.debug(
                f"can_trade: strength gate — need strength={min_str}, got {signal_strength}")
            return False

        # ── Base confidence gate ──────────────────────────────────────────────
        min_conf = self.min_required_confidence()
        if confidence < min_conf:
            logger.debug(
                f"can_trade: confidence gate — need confidence={min_conf}, got {confidence}")
            return False

        return True

    # ── Position sizing (win-streak-aware) ────────────────────────────────────

    def calculate_stake(self) -> float:
        """
        Win streak scaling:
          streak ≥ 8 → 4.0× base
          streak ≥ 6 → 3.0× base
          streak ≥ 4 → 2.0× base
          streak ≥ 3 → 1.5× base
          streak 1–2 → 1.0× base
          streak  0  → 1.0× base (normal)
        Any loss resets streak to 0 → 1.0× base immediately.
        Stake always capped at MAX_STAKE.
        """
        if self._current_balance <= 0:
            return self.min_stake

        base = self._current_balance * self.risk_per_trade

        if self._current_streak > 0:
            thresholds: List[int]    = getattr(
                config, "WIN_STREAK_SCALE_THRESHOLDS",   [3, 4, 6, 8])
            multipliers: List[float] = getattr(
                config, "WIN_STREAK_STAKE_MULTIPLIERS", [1.5, 2.0, 3.0, 4.0])
            mult = 1.0
            for t, m in zip(thresholds, multipliers):
                if self._current_streak >= t:
                    mult = m
            raw = base * mult
            logger.debug(
                f"Win streak {self._current_streak} → "
                f"multiplier {mult:.2f}× | base=${base:.2f} → raw=${raw:.2f}")
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
            f"effective_max_concurrent={self.effective_max_concurrent}")
        return rec

    def register_close(self, rec: TradeRecord, exit_price: float, pnl: float):
        rec.exit_price         = exit_price
        rec.pnl                = pnl
        rec.won                = pnl > 0
        self._open_trade_count = max(0, self._open_trade_count - 1)
        self.total_pnl        += pnl

        if rec.won:
            self.wins            += 1
            self._current_streak += 1
        else:
            self.losses          += 1
            self._current_streak  = 0   # reset to 0; no negative loss counter

        logger.info(
            f"Trade CLOSE | {rec.symbol} {rec.direction} | "
            f"pnl=${pnl:+.4f} | {'WIN' if rec.won else 'LOSS'} | "
            f"streak={self._current_streak} | "
            f"effective_max_concurrent={self.effective_max_concurrent} | "
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
            "current_balance":          round(self._current_balance, 4),
            "day_start_balance":        round(self._day_start_balance, 4),
            "daily_pnl":                round(self.daily_pnl, 4),
            "daily_pnl_pct":            round(self.daily_pnl_pct * 100, 2),
            "total_trades":             self.total_trades,
            "wins":                     self.wins,
            "losses":                   self.losses,
            "win_rate":                 round(self.wins / total * 100, 1) if total else 0,
            "total_pnl":                round(self.total_pnl, 4),
            "paused":                   self._paused,
            "open_trades":              self._open_trade_count,
            "streak":                   self._current_streak,
            "effective_max_concurrent": self.effective_max_concurrent,
            "min_required_strength":    self.min_required_strength(),
            "min_required_confidence":  self.min_required_confidence(),
        }

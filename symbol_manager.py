"""
symbol_manager.py – Symbol rotation with per-loss cooldown + cycle-based suspension.

v4 → v5 changes (FIX 3):
  • Win suspension added:
      - suspend_win(symbol)  : suspend a symbol for SYMBOL_WIN_SUSPENSION_CYCLES
        (default 2) full trading cycles after a confirmed WIN.
      - suspend_loss(symbol) : suspend a symbol for SYMBOL_LOSS_SUSPENSION_CYCLES
        (default 3) full trading cycles after a confirmed LOSS.
        (replaces the old suspend(symbol, cycles=2) API which only handled losses)
      - Public suspend(symbol, cycles) still available for direct use.
      - record_trade() now calls suspend_win() on WIN and suspend_loss() on LOSS
        automatically, in addition to the existing time-based cooldown.

  • Never trade the same symbol twice in the same cycle:
      - get_queue() now honours a _used_this_cycle set.
      - mark_used(symbol)  : called by bot_engine after executing a trade.
      - reset_cycle_used() : called by bot_engine at the start of each new cycle.
        Symbols in _used_this_cycle are excluded from the queue entirely
        for the remainder of that cycle, even if slots are unfilled.

  • decrement_suspensions() logs all currently suspended symbols at call time
    (satisfies "Log suspended symbols at start of every cycle").

  All v4 logic unchanged: time-based cooldown, session hours, scoring,
  queue generation, dashboard helpers.
"""

import time
import logging
import datetime
from typing import List, Dict, Set
from dataclasses import dataclass, field

import symbols as sym_module
import config

logger = logging.getLogger(__name__)

SESSIONS = {
    "sydney":   (21, 0,  6, 0),
    "tokyo":    ( 0, 0,  9, 0),
    "london":   ( 7, 0, 16, 0),
    "new_york": (12, 0, 21, 0),
}

SYMBOL_SESSION: Dict[str, List[str]] = {
    "frxEURUSD": ["london", "new_york"],
    "frxGBPUSD": ["london", "new_york"],
    "frxUSDJPY": ["tokyo",  "new_york"],
    "frxAUDUSD": ["sydney", "london"],
    "frxUSDCAD": ["new_york"],
    "frxUSDCHF": ["london", "new_york"],
    "frxNZDUSD": ["sydney"],
    "frxEURGBP": ["london"],
    "frxEURJPY": ["london", "tokyo"],
    "frxGBPJPY": ["london", "tokyo"],
    "frxXAUUSD": ["london", "new_york"],
    "frxXAGUSD": ["london", "new_york"],
    "frxUSOIL":  ["new_york"],
    "frxUKOIL":  ["london"],
    "cryBTCUSD": [],
    "cryETHUSD": [],
}
for s in sym_module.SYNTHETIC:
    SYMBOL_SESSION[s] = []   # synthetics trade 24/7

# Build a fast-lookup set of synthetic symbol names
_SYNTHETIC_SET: Set[str] = set(sym_module.SYNTHETIC)


@dataclass
class SymbolStats:
    symbol:         str
    trades:         int   = 0
    wins:           int   = 0
    total_pnl:      float = 0.0
    last_trade_ts:  float = 0.0
    # Epoch until which this symbol is blocked after a loss (time-based)
    cooldown_until: float = 0.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades) if self.trades > 0 else 0.5

    @property
    def score(self) -> float:
        recency_bonus = 0.1 if time.time() - self.last_trade_ts > 3600 else 0
        pnl_score     = min(self.total_pnl * 10, 1.0)
        return self.win_rate + pnl_score + recency_bonus

    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    def cooldown_remaining_secs(self) -> float:
        return max(0.0, self.cooldown_until - time.time())


class SymbolManager:

    def __init__(self, refresh_interval: int = 3600):
        self._refresh_interval = refresh_interval
        self._stats:        Dict[str, SymbolStats] = {}
        self._active:       Set[str] = set(sym_module.SYNTHETIC)
        self._last_refresh: float    = 0.0

        # ── Cycle-based suspension counter ─────────────────────────────────────
        # Key: symbol str  Value: number of cycles remaining before re-entry
        self._suspend_cycles: Dict[str, int] = {}

        # ── Per-cycle deduplication ────────────────────────────────────────────
        # Symbols traded this cycle — excluded from queue until reset_cycle_used()
        self._used_this_cycle: Set[str] = set()

        for sym in sym_module.ALL_SYMBOLS:
            self._stats[sym] = SymbolStats(symbol=sym)

    # ── Active symbol list ────────────────────────────────────────────────────

    def update_active(self, active_symbols_from_api: List[str]):
        self._active       = set(active_symbols_from_api) | set(sym_module.SYNTHETIC)
        self._last_refresh = time.time()
        logger.info(f"SymbolManager: {len(self._active)} active symbols")

    def needs_refresh(self) -> bool:
        return time.time() - self._last_refresh > self._refresh_interval

    # ── Session hours ─────────────────────────────────────────────────────────

    def _in_session(self, symbol: str) -> bool:
        sessions = SYMBOL_SESSION.get(symbol, None)
        if sessions is None or sessions == []:
            return True
        now_utc = datetime.datetime.utcnow()
        now_min = now_utc.hour * 60 + now_utc.minute
        for sess_name in sessions:
            oh, om, ch, cm = SESSIONS[sess_name]
            open_min  = oh * 60 + om
            close_min = ch * 60 + cm
            if open_min < close_min:
                if open_min <= now_min <= close_min:
                    return True
            else:
                if now_min >= open_min or now_min <= close_min:
                    return True
        return False

    # ── Per-cycle deduplication API ───────────────────────────────────────────

    def mark_used(self, symbol: str):
        """
        Mark *symbol* as traded in the current cycle.
        Called by bot_engine immediately after a trade is executed.
        Symbols marked used are excluded from the queue for the rest of this cycle.
        """
        self._used_this_cycle.add(symbol)
        logger.debug(f"SymbolManager: {symbol} marked used this cycle")

    def reset_cycle_used(self):
        """
        Clear the per-cycle used-symbol set.
        Called by bot_engine at the START of each new trading cycle, before scanning.
        """
        if self._used_this_cycle:
            logger.debug(
                f"SymbolManager: resetting cycle-used set "
                f"({len(self._used_this_cycle)} symbol(s))")
        self._used_this_cycle.clear()

    # ── Cycle-based suspension API ────────────────────────────────────────────

    def suspend(self, symbol: str, cycles: int = 2):
        """
        Suspend *symbol* for *cycles* full trading cycles.
        If the symbol is already suspended, the counter is reset to *cycles*
        (a fresh event restarts the penalty, not stacks it).
        """
        cycles = max(1, cycles)
        self._suspend_cycles[symbol] = cycles
        logger.info(
            f"SymbolManager: {symbol} SUSPENDED for {cycles} cycle(s)")

    def suspend_win(self, symbol: str):
        """
        Suspend *symbol* after a WIN for SYMBOL_WIN_SUSPENSION_CYCLES cycles (default 2).
        Called automatically by record_trade() on a win.
        """
        cycles = getattr(config, "SYMBOL_WIN_SUSPENSION_CYCLES", 2)
        cycles = max(1, cycles)
        self._suspend_cycles[symbol] = cycles
        logger.info(
            f"SymbolManager: {symbol} SUSPENDED for {cycles} cycle(s) after WIN")

    def suspend_loss(self, symbol: str):
        """
        Suspend *symbol* after a LOSS for SYMBOL_LOSS_SUSPENSION_CYCLES cycles (default 3).
        Called automatically by record_trade() on a loss.
        """
        cycles = getattr(config, "SYMBOL_LOSS_SUSPENSION_CYCLES", 3)
        cycles = max(1, cycles)
        self._suspend_cycles[symbol] = cycles
        logger.info(
            f"SymbolManager: {symbol} SUSPENDED for {cycles} cycle(s) after LOSS")

    def clear_suspension(self, symbol: str):
        """
        Immediately lift the suspension for *symbol*.
        No-op if the symbol is not suspended.
        """
        if symbol in self._suspend_cycles and self._suspend_cycles[symbol] > 0:
            self._suspend_cycles.pop(symbol, None)
            logger.info(
                f"SymbolManager: {symbol} suspension CLEARED")

    def is_suspended(self, symbol: str) -> bool:
        """Return True while the symbol has cycles_remaining > 0."""
        return self._suspend_cycles.get(symbol, 0) > 0

    def decrement_suspensions(self):
        """
        Called by bot_engine at the START of each new trading cycle, before
        scanning.  Decrements every active counter by 1 and removes any
        counter that reaches zero (symbol re-enters the pool immediately).
        Logs all currently suspended symbols for Render log visibility.
        """
        # Log suspended symbols BEFORE decrementing
        suspended = self.suspended_symbols()
        if suspended:
            logger.info(
                f"SymbolManager: suspended symbols entering this cycle: "
                f"{suspended} (will decrement counters now)")
        else:
            logger.info("SymbolManager: no suspended symbols this cycle")

        to_clear = []
        for sym, remaining in list(self._suspend_cycles.items()):
            new_val = remaining - 1
            if new_val <= 0:
                to_clear.append(sym)
            else:
                self._suspend_cycles[sym] = new_val
        for sym in to_clear:
            self._suspend_cycles.pop(sym, None)
            logger.info(
                f"SymbolManager: {sym} suspension expired – re-entering pool")

    def suspended_symbols(self) -> List[str]:
        """Return list of currently suspended symbols (for Render log visibility)."""
        return [sym for sym, rem in self._suspend_cycles.items() if rem > 0]

    def suspension_cycles_remaining(self, symbol: str) -> int:
        """Return how many cycles *symbol* is still suspended for (0 = not suspended)."""
        return self._suspend_cycles.get(symbol, 0)

    # ── Trade outcome recording ───────────────────────────────────────────────

    def record_trade(self, symbol: str, won: bool, pnl: float):
        if symbol not in self._stats:
            self._stats[symbol] = SymbolStats(symbol=symbol)

        st               = self._stats[symbol]
        st.trades       += 1
        st.wins         += int(won)
        st.total_pnl    += pnl
        st.last_trade_ts = time.time()

        if won:
            # Suspend winning symbol for SYMBOL_WIN_SUSPENSION_CYCLES cycles
            self.suspend_win(symbol)
        else:
            # Time-based cooldown (unchanged from v4)
            if symbol in _SYNTHETIC_SET:
                cooldown_secs = config.SYNTHETIC_LOSS_COOLDOWN_SECONDS
            else:
                cooldown_secs = config.SYMBOL_LOSS_COOLDOWN_SECONDS

            st.cooldown_until = time.time() + cooldown_secs
            logger.info(
                f"SymbolManager: {symbol} on {cooldown_secs}s time-cooldown after LOSS "
                f"(pnl=${pnl:+.4f})")

            # Suspend losing symbol for SYMBOL_LOSS_SUSPENSION_CYCLES cycles
            self.suspend_loss(symbol)

    # ── Cooldown status (time-based, public, used by bot_engine) ─────────────

    def is_in_cooldown(self, symbol: str) -> bool:
        st = self._stats.get(symbol)
        return st.in_cooldown() if st else False

    def cooldown_remaining(self, symbol: str) -> float:
        st = self._stats.get(symbol)
        return st.cooldown_remaining_secs() if st else 0.0

    # ── Queue generation ──────────────────────────────────────────────────────

    def get_queue(self, max_symbols: int = 200) -> List[str]:
        """
        Return active symbols sorted by (session-open first, score desc).
        Symbols currently in their time-based loss cooldown are excluded.
        Symbols marked used this cycle (_used_this_cycle) are excluded entirely
        — never trade the same symbol twice in the same cycle.
        Cycle-suspended symbols are NOT excluded here — bot_engine filters
        them out via is_suspended() before scanning.
        """
        candidates = []
        for sym in sym_module.ALL_SYMBOLS:
            if sym not in self._active:
                continue
            # Never trade the same symbol twice in the same cycle
            if sym in self._used_this_cycle:
                continue
            st = self._stats.get(sym)
            if st and st.in_cooldown():
                continue
            in_sess = self._in_session(sym)
            score   = st.score if st else 0.5
            candidates.append((sym, in_sess, score))

        candidates.sort(key=lambda x: (not x[1], -x[2]))
        queue = [c[0] for c in candidates[:max_symbols]]

        # Always ensure at least 3 synthetics are at the front of the queue
        synth_in_q = [s for s in queue if s in _SYNTHETIC_SET]
        if len(synth_in_q) < 3:
            extras = [s for s in sym_module.SYNTHETIC[:3]
                      if s not in queue and s not in self._used_this_cycle]
            queue  = extras + queue

        return queue

    # ── Dashboard helpers ─────────────────────────────────────────────────────

    def best_symbols(self, n: int = 5) -> List[dict]:
        scored = sorted(
            [(s, st) for s, st in self._stats.items() if st.trades > 0],
            key=lambda x: x[1].score, reverse=True)
        return [
            {"symbol": s, "trades": st.trades,
             "win_rate": round(st.win_rate * 100, 1),
             "pnl": round(st.total_pnl, 4),
             "score": round(st.score, 3),
             "cooldown_secs": round(st.cooldown_remaining_secs(), 0),
             "suspend_cycles": self._suspend_cycles.get(s, 0)}
            for s, st in scored[:n]
        ]

    def all_stats(self) -> Dict[str, dict]:
        return {
            s: {"trades": st.trades, "wins": st.wins,
                "win_rate": round(st.win_rate * 100, 1),
                "pnl": round(st.total_pnl, 4),
                "cooldown": st.in_cooldown(),
                "suspended_cycles": self._suspend_cycles.get(s, 0)}
            for s, st in self._stats.items() if st.trades > 0
        }

    @property
    def current_session(self) -> str:
        active  = []
        now_utc = datetime.datetime.utcnow()
        now_min = now_utc.hour * 60 + now_utc.minute
        for name, (oh, om, ch, cm) in SESSIONS.items():
            open_min  = oh * 60 + om
            close_min = ch * 60 + cm
            if open_min < close_min:
                if open_min <= now_min <= close_min:
                    active.append(name.capitalize())
            else:
                if now_min >= open_min or now_min <= close_min:
                    active.append(name.capitalize())
        return " + ".join(active) if active else "Off-hours (synthetics only)"

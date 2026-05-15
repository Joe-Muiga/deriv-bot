"""
symbol_manager.py – Symbol rotation with per-loss cooldown.

After ANY losing trade on a symbol, that symbol is excluded from the scan
queue for SYMBOL_LOSS_COOLDOWN_SECONDS (15 minutes).  Winning trades never
trigger a cooldown.  Consecutive-loss suppression (the old logic) is gone;
only individual-loss cooldowns exist.  The score field is for dashboard
display and queue ordering only — it never permanently blocks a symbol.
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
    SYMBOL_SESSION[s] = []


@dataclass
class SymbolStats:
    symbol:         str
    trades:         int   = 0
    wins:           int   = 0
    total_pnl:      float = 0.0
    last_trade_ts:  float = 0.0
    # Epoch until which this symbol is blocked after a loss
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

        for sym in sym_module.ALL_SYMBOLS:
            self._stats[sym] = SymbolStats(symbol=sym)

    # ── Active symbol list ────────────────────────────────────────────────────

    def update_active(self, active_symbols_from_api: List[str]):
        self._active      = set(active_symbols_from_api) | set(sym_module.SYNTHETIC)
        self._last_refresh = time.time()
        logger.info(f"SymbolManager: {len(self._active)} active symbols")

    def needs_refresh(self) -> bool:
        return time.time() - self._last_refresh > self._refresh_interval

    # ── Session hours ─────────────────────────────────────────────────────────

    def _in_session(self, symbol: str) -> bool:
        sessions = SYMBOL_SESSION.get(symbol, None)
        if sessions is None or sessions == []:
            return True
        now_min = datetime.datetime.utcnow().hour * 60 + datetime.datetime.utcnow().minute
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

    # ── Trade outcome recording ───────────────────────────────────────────────

    def record_trade(self, symbol: str, won: bool, pnl: float):
        if symbol not in self._stats:
            self._stats[symbol] = SymbolStats(symbol=symbol)

        st               = self._stats[symbol]
        st.trades       += 1
        st.wins         += int(won)
        st.total_pnl    += pnl
        st.last_trade_ts = time.time()

        if not won:
            # Apply 15-minute cooldown on this symbol after any loss
            st.cooldown_until = time.time() + config.SYMBOL_LOSS_COOLDOWN_SECONDS
            logger.info(
                f"SymbolManager: {symbol} on 15-min cooldown after LOSS "
                f"(pnl=${pnl:+.4f})")
        # No cooldown on wins — symbol immediately eligible for the next cycle

    # ── Cooldown status (public, used by bot_engine) ──────────────────────────

    def is_in_cooldown(self, symbol: str) -> bool:
        """Returns True if this symbol must not be traded right now."""
        st = self._stats.get(symbol)
        return st.in_cooldown() if st else False

    def cooldown_remaining(self, symbol: str) -> float:
        st = self._stats.get(symbol)
        return st.cooldown_remaining_secs() if st else 0.0

    # ── Queue generation ──────────────────────────────────────────────────────

    def get_queue(self, max_symbols: int = 200) -> List[str]:
        """
        Return active symbols sorted by (session-open first, score desc).
        Symbols currently in their 15-min loss cooldown are excluded.
        This is called every scan cycle so cooldowns apply in real time.
        """
        candidates = []
        for sym in sym_module.ALL_SYMBOLS:
            if sym not in self._active:
                continue
            st = self._stats.get(sym)
            if st and st.in_cooldown():
                continue           # blocked — lost on this symbol recently
            in_sess = self._in_session(sym)
            score   = st.score if st else 0.5
            candidates.append((sym, in_sess, score))

        candidates.sort(key=lambda x: (not x[1], -x[2]))
        queue = [c[0] for c in candidates[:max_symbols]]

        if not any(s in sym_module.SYNTHETIC for s in queue):
            queue = list(sym_module.SYNTHETIC[:3]) + queue

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
             "cooldown_secs": round(st.cooldown_remaining_secs(), 0)}
            for s, st in scored[:n]
        ]

    def all_stats(self) -> Dict[str, dict]:
        return {
            s: {"trades": st.trades, "wins": st.wins,
                "win_rate": round(st.win_rate * 100, 1),
                "pnl": round(st.total_pnl, 4),
                "cooldown": st.in_cooldown()}
            for s, st in self._stats.items() if st.trades > 0
        }

    @property
    def current_session(self) -> str:
        active  = []
        now_min = datetime.datetime.utcnow().hour * 60 + datetime.datetime.utcnow().minute
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

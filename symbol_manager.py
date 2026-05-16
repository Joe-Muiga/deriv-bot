"""
symbol_manager.py – Symbol rotation with per-loss cooldown.

v2 → v3 changes:
  • Synthetic instruments (R_10, R_25, R_50, R_75, R_100, BOOM, CRASH, etc.)
    now use SYNTHETIC_LOSS_COOLDOWN_SECONDS (120 s = 2 min) instead of
    SYMBOL_LOSS_COOLDOWN_SECONDS (180 s = 3 min).
    Reasoning: 1-min chart synthetic symbols can reverse or continue in a new
    direction within 2–3 bars — a 15-minute (old) or even 3-minute cooldown on
    a 1-min chart is far too long and kills re-entry opportunities.

  • record_trade() detects whether a symbol is synthetic via
    sym_module.SYNTHETIC membership and applies the shorter cooldown.

  • Consecutive-loss suppression is still absent; only individual-loss
    cooldowns exist (carried over from v2).

  • Score and queue ordering unchanged.
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
            # Synthetics: 2-minute cooldown.  All other instruments: 3 minutes.
            if symbol in _SYNTHETIC_SET:
                cooldown_secs = config.SYNTHETIC_LOSS_COOLDOWN_SECONDS
            else:
                cooldown_secs = config.SYMBOL_LOSS_COOLDOWN_SECONDS

            st.cooldown_until = time.time() + cooldown_secs
            logger.info(
                f"SymbolManager: {symbol} on {cooldown_secs}s cooldown after LOSS "
                f"(pnl=${pnl:+.4f})")
        # Wins never trigger a cooldown

    # ── Cooldown status (public, used by bot_engine) ──────────────────────────

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
        Symbols currently in their loss cooldown are excluded.
        """
        candidates = []
        for sym in sym_module.ALL_SYMBOLS:
            if sym not in self._active:
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
            extras = [s for s in sym_module.SYNTHETIC[:3] if s not in queue]
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

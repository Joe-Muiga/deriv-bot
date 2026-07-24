"""
strategy_stats.py

Tracks closed-trade outcomes per (strategy, symbol) pair, maintains a
rolling win rate over a configurable window, and flags underperforming
pairs using config.STRATEGY_WIN_RATE_FLOOR / STRATEGY_WIN_RATE_MIN_TRADES.

Persistence: SQLite if the sqlite3 module + a writable DB file are
available, otherwise falls back to a local JSON file. Either way, stats
survive Render restarts as long as the underlying disk/volume persists
(ephemeral filesystems will still lose data on redeploy — mount a disk
if you need cross-deploy durability).

Thread-safety: a single RLock guards all read/write access, since
bot_engine.py may call record_trade() from multiple async tasks and the
dashboard route may read concurrently.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import config

try:
    import sqlite3
    SQLITE_AVAILABLE = True
except ImportError:
    SQLITE_AVAILABLE = False


# ── STORAGE LOCATIONS ─────────────────────────────────────────
DATA_DIR = os.environ.get("STRATEGY_STATS_DIR", os.path.dirname(os.path.abspath(__file__)))
SQLITE_PATH = os.path.join(DATA_DIR, "strategy_stats.db")
JSON_PATH = os.path.join(DATA_DIR, "strategy_stats.json")

DEFAULT_WINDOW = 100
WIN_RATE_FLOOR = config.STRATEGY_WIN_RATE_FLOOR
MIN_TRADES_FOR_FLOOR = config.STRATEGY_WIN_RATE_MIN_TRADES


@dataclass
class TradeRecord:
    strategy: str
    symbol: str
    entry_score: float
    won: bool
    stake: float
    payout: float
    timestamp: float  # unix epoch seconds


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """
    Wilson score confidence interval for a binomial proportion.
    z=1.96 -> ~95% CI. Returns (low, high), both in [0, 1].
    With n=0 returns (0.0, 1.0) — maximally uncertain.
    """
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + (z ** 2) / n
    center = p + (z ** 2) / (2 * n)
    margin = z * math.sqrt((p * (1 - p) / n) + (z ** 2) / (4 * n ** 2))
    low = (center - margin) / denom
    high = (center + margin) / denom
    return max(0.0, low), min(1.0, high)


class _SqliteBackend:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> "sqlite3.Connection":
        # sqlite3 connections aren't safe to share across threads by
        # default; keep one per thread, guarded externally by the RLock.
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            self._local.conn = conn
        return conn

    def _init_db(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                entry_score REAL,
                won INTEGER NOT NULL,
                stake REAL,
                payout REAL,
                timestamp REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategy_symbol_ts "
            "ON trades (strategy, symbol, timestamp)"
        )
        conn.commit()
        conn.close()

    def insert(self, rec: TradeRecord):
        conn = self._conn()
        conn.execute(
            "INSERT INTO trades (strategy, symbol, entry_score, won, stake, payout, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rec.strategy, rec.symbol, rec.entry_score, int(rec.won), rec.stake, rec.payout, rec.timestamp),
        )
        conn.commit()

    def recent_results(self, strategy: str, symbol: str, window: int) -> List[bool]:
        conn = self._conn()
        cur = conn.execute(
            "SELECT won FROM trades WHERE strategy=? AND symbol=? "
            "ORDER BY timestamp DESC LIMIT ?",
            (strategy, symbol, window),
        )
        return [bool(row[0]) for row in cur.fetchall()]

    def count(self, strategy: str, symbol: str) -> int:
        conn = self._conn()
        cur = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE strategy=? AND symbol=?",
            (strategy, symbol),
        )
        return cur.fetchone()[0]

    def all_pairs(self) -> List[Tuple[str, str]]:
        conn = self._conn()
        cur = conn.execute("SELECT DISTINCT strategy, symbol FROM trades")
        return [(row[0], row[1]) for row in cur.fetchall()]


class _JsonBackend:
    """
    Fallback backend. Stores trades as a flat list per (strategy, symbol)
    key in a single JSON file. Fine for demo/research-scale trade counts;
    not intended for very high volume (whole file is rewritten on each
    insert).
    """

    def __init__(self, path: str):
        self.path = path
        if not os.path.exists(self.path):
            self._write({})

    def _read(self) -> Dict[str, List[dict]]:
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: Dict[str, List[dict]]):
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, self.path)  # atomic on POSIX

    @staticmethod
    def _key(strategy: str, symbol: str) -> str:
        return f"{strategy}::{symbol}"

    def insert(self, rec: TradeRecord):
        data = self._read()
        key = self._key(rec.strategy, rec.symbol)
        data.setdefault(key, [])
        data[key].append(asdict(rec))
        self._write(data)

    def recent_results(self, strategy: str, symbol: str, window: int) -> List[bool]:
        data = self._read()
        rows = data.get(self._key(strategy, symbol), [])
        rows_sorted = sorted(rows, key=lambda r: r["timestamp"], reverse=True)
        return [bool(r["won"]) for r in rows_sorted[:window]]

    def count(self, strategy: str, symbol: str) -> int:
        data = self._read()
        return len(data.get(self._key(strategy, symbol), []))

    def all_pairs(self) -> List[Tuple[str, str]]:
        data = self._read()
        pairs = []
        for key in data.keys():
            if "::" in key:
                strategy, symbol = key.split("::", 1)
                pairs.append((strategy, symbol))
        return pairs


class StrategyStats:
    def __init__(self):
        self._lock = threading.RLock()
        self._backend = None
        self._backend_name = None
        self._init_backend()

    def _init_backend(self):
        if SQLITE_AVAILABLE:
            try:
                self._backend = _SqliteBackend(SQLITE_PATH)
                self._backend_name = "sqlite"
                return
            except sqlite3.Error:
                pass  # fall through to JSON
        self._backend = _JsonBackend(JSON_PATH)
        self._backend_name = "json"

    # ── PUBLIC API ────────────────────────────────────────────

    def record_trade(
        self,
        strategy: str,
        symbol: str,
        entry_score: float,
        won: bool,
        stake: float,
        payout: float,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record a closed trade outcome. Call this whenever a trade closes."""
        rec = TradeRecord(
            strategy=strategy,
            symbol=symbol,
            entry_score=float(entry_score),
            won=bool(won),
            stake=float(stake),
            payout=float(payout),
            timestamp=float(timestamp) if timestamp is not None else time.time(),
        )
        with self._lock:
            self._backend.insert(rec)

    def get_win_rate(
        self, strategy: str, symbol: str, window: int = DEFAULT_WINDOW
    ) -> Tuple[float, float, float, int]:
        """
        Rolling win rate over the most recent `window` trades for this
        (strategy, symbol) pair, with a Wilson 95% confidence interval.

        Returns (rate, ci_low, ci_high, n) where n is the number of
        trades the rate was computed over (<= window). With n=0, returns
        (0.0, 0.0, 1.0, 0).
        """
        with self._lock:
            results = self._backend.recent_results(strategy, symbol, window)
        n = len(results)
        if n == 0:
            return 0.0, 0.0, 1.0, 0
        wins = sum(results)
        rate = wins / n
        ci_low, ci_high = _wilson_interval(wins, n)
        return rate, ci_low, ci_high, n

    def is_underperforming(self, strategy: str, symbol: str) -> bool:
        """
        True if this (strategy, symbol) pair has >= STRATEGY_WIN_RATE_MIN_TRADES
        logged trades AND its rolling win rate (window=STRATEGY_WIN_RATE_MIN_TRADES)
        falls below config.STRATEGY_WIN_RATE_FLOOR. Small samples are never
        flagged, regardless of how bad the raw rate looks.
        """
        with self._lock:
            total = self._backend.count(strategy, symbol)
        if total < MIN_TRADES_FOR_FLOOR:
            return False
        rate, _ci_low, _ci_high, n = self.get_win_rate(
            strategy, symbol, window=MIN_TRADES_FOR_FLOOR
        )
        if n < MIN_TRADES_FOR_FLOOR:
            return False
        return rate < WIN_RATE_FLOOR

    def all_stats(self, window: int = DEFAULT_WINDOW) -> List[dict]:
        """
        Returns a list of dicts, one per (strategy, symbol) pair seen so
        far, for consumption by a dashboard route. Each dict:
            {
                "strategy": str,
                "symbol": str,
                "win_rate": float,
                "ci_low": float,
                "ci_high": float,
                "n": int,               # trades in the rolling window
                "total_trades": int,    # all-time trades for this pair
                "underperforming": bool,
            }
        """
        with self._lock:
            pairs = self._backend.all_pairs()

        out = []
        for strategy, symbol in pairs:
            rate, ci_low, ci_high, n = self.get_win_rate(strategy, symbol, window=window)
            total = self._backend.count(strategy, symbol)
            out.append(
                {
                    "strategy": strategy,
                    "symbol": symbol,
                    "win_rate": round(rate, 4),
                    "ci_low": round(ci_low, 4),
                    "ci_high": round(ci_high, 4),
                    "n": n,
                    "total_trades": total,
                    "underperforming": self.is_underperforming(strategy, symbol),
                }
            )
        out.sort(key=lambda d: (d["strategy"], d["symbol"]))
        return out

    @property
    def backend_name(self) -> str:
        return self._backend_name


# Module-level singleton — import and use directly:
#   from strategy_stats import stats
#   stats.record_trade(...)
stats = StrategyStats()


if __name__ == "__main__":
    # Quick smoke test
    import random

    random.seed(0)
    for i in range(120):
        stats.record_trade(
            strategy="mean_reversion",
            symbol="R_75",
            entry_score=round(random.uniform(0.6, 0.9), 3),
            won=random.random() < 0.52,
            stake=1.0,
            payout=1.9 if random.random() < 0.52 else 0.0,
            timestamp=time.time() - (120 - i),
        )
    print("Backend:", stats.backend_name)
    print("Win rate:", stats.get_win_rate("mean_reversion", "R_75"))
    print("Underperforming:", stats.is_underperforming("mean_reversion", "R_75"))
    print("All stats:", stats.all_stats())

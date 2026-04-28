"""
news_filter.py – Block trades 30 minutes before high-impact economic events.

Primary source: market-calendar-tool (Forex Factory scraper).
Fallback: manual check on well-known fixed-schedule events (NFP, FOMC, CPI).
"""

import logging
import datetime
import time
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

# Symbols to monitor for news (currency codes)
WATCHED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}
HIGH_IMPACT_LABELS = {"high", "red", "3"}   # different FF representations

# Map Deriv symbol prefix → currency pair
SYMBOL_CURRENCIES = {
    "frxEURUSD": {"EUR", "USD"},
    "frxGBPUSD": {"GBP", "USD"},
    "frxUSDJPY": {"USD", "JPY"},
    "frxAUDUSD": {"AUD", "USD"},
    "frxUSDCAD": {"USD", "CAD"},
    "frxUSDCHF": {"USD", "CHF"},
    "frxNZDUSD": {"NZD", "USD"},
    "frxXAUUSD": {"USD"},      # Gold vs USD
    "frxUSOIL":  {"USD"},
    "cryBTCUSD": {"USD"},
    "cryETHUSD": {"USD"},
}


class NewsFilter:
    """
    Fetches the Forex Factory calendar once per hour and caches upcoming events.
    `is_blocked(symbol)` returns True if a high-impact event is within
    BLOCK_MINUTES of the current time for any currency related to that symbol.
    """

    def __init__(self, block_minutes: int = 30, refresh_minutes: int = 60):
        self.block_minutes   = block_minutes
        self.refresh_minutes = refresh_minutes
        self._events: List[dict] = []
        self._last_fetch: float  = 0.0
        self._lock = threading.Lock()
        self._available = self._check_calendar_tool()

    def _check_calendar_tool(self) -> bool:
        try:
            from market_calendar_tool import scrape_calendar
            logger.info("market-calendar-tool available ✓")
            return True
        except ImportError:
            logger.warning("market-calendar-tool not installed. "
                           "News filter will use fallback schedule.")
            return False

    def refresh(self):
        """Fetch / refresh the event list."""
        now = time.time()
        if now - self._last_fetch < self.refresh_minutes * 60:
            return   # still fresh

        if self._available:
            self._fetch_from_calendar_tool()
        else:
            self._fetch_fallback()

        self._last_fetch = time.time()

    def _fetch_from_calendar_tool(self):
        try:
            from market_calendar_tool import scrape_calendar
            df = scrape_calendar()
            events = []
            for _, row in df.iterrows():
                impact = str(row.get("impact", "")).lower().strip()
                if impact not in HIGH_IMPACT_LABELS:
                    continue
                currency = str(row.get("currency", "")).upper().strip()
                try:
                    # Parse date + time into a datetime
                    date_str = str(row["date"])
                    time_str = str(row["time"])
                    dt = datetime.datetime.strptime(
                        f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                    events.append({
                        "dt":       dt,
                        "currency": currency,
                        "title":    str(row.get("title", "")),
                        "impact":   impact,
                    })
                except Exception:
                    continue
            with self._lock:
                self._events = events
            logger.info(f"News filter: loaded {len(events)} high-impact events")
        except Exception as exc:
            logger.error(f"Calendar fetch failed: {exc}")

    def _fetch_fallback(self):
        """
        Hard-coded monthly schedule for major recurring events.
        These are approximate; real dates vary.  Use only when the
        calendar tool is unavailable.
        """
        now  = datetime.datetime.utcnow()
        year = now.year
        month = now.month

        # First Friday of the month ≈ NFP at 13:30 UTC
        first_day = datetime.datetime(year, month, 1)
        first_fri = first_day + datetime.timedelta(days=(4 - first_day.weekday()) % 7)
        events = [
            {"dt": first_fri.replace(hour=13, minute=30), "currency": "USD",
             "title": "Non-Farm Payrolls", "impact": "high"},
        ]
        with self._lock:
            self._events = events

    def is_blocked(self, symbol: str) -> bool:
        """
        Returns True if trading should be blocked right now for `symbol`
        because a high-impact event is within block_minutes.
        """
        self.refresh()
        now     = datetime.datetime.utcnow()
        window  = datetime.timedelta(minutes=self.block_minutes)

        relevant_currencies = SYMBOL_CURRENCIES.get(symbol, {"USD"})

        with self._lock:
            for ev in self._events:
                if ev["currency"] not in relevant_currencies:
                    continue
                diff = (ev["dt"] - now).total_seconds()
                if -300 <= diff <= self.block_minutes * 60:   # -5 min to +block_min
                    logger.info(f"News block: {ev['title']} ({ev['currency']}) "
                                f"in {diff/60:.1f} min | blocking {symbol}")
                    return True
        return False

    def next_events(self, n: int = 5) -> List[dict]:
        """Return the next N high-impact events."""
        now = datetime.datetime.utcnow()
        with self._lock:
            upcoming = sorted(
                [e for e in self._events if e["dt"] >= now],
                key=lambda x: x["dt"])
        return upcoming[:n]

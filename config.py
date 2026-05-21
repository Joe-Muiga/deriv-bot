"""
config.py – Centralised configuration for the Deriv Trading Bot.
v16 — Range Break + Boom/Crash only. Volatility indices excluded from trading.
"""

import os
from typing import List
from dotenv import load_dotenv

load_dotenv()

# ─── Deriv API ────────────────────────────────────────────────────────────────
DERIV_APP_ID    : str = os.environ.get("DERIV_APP_ID", "")
DERIV_API_TOKEN : str = os.environ.get("DERIV_API_TOKEN", "")
DERIV_WS_URL    : str = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
DERIV_CURRENCY  : str = "USD"

# ─── WebSocket reconnect ──────────────────────────────────────────────────────
WEBSOCKET_RECONNECT_INTERVAL : int = 10
WEBSOCKET_MAX_RECONNECTS     : int = 5

# ─── Strategy symbols only — no volatility indices for trading ────────────────
RANGE_BREAK_SYMBOLS : List[str] = ["RDBULL", "RDBEAR"]
BOOM_CRASH_SYMBOLS  : List[str] = ["BOOM500", "BOOM1000", "CRASH500", "CRASH1000", "BOOM300", "CRASH300"]
TRADE_SYMBOLS       : List[str] = RANGE_BREAK_SYMBOLS + BOOM_CRASH_SYMBOLS

# Volatility indices kept for reference but NEVER traded
VOLATILITY_SYMBOLS  : List[str] = ["R_10", "R_25", "R_50", "R_75", "R_100"]

# ─── Timeframes ───────────────────────────────────────────────────────────────
LTF_GRANULARITY : int = 60
LTF_BARS        : int = 60   # fetch 60 bars (need 50 for strategy logic)

# ─── Session UTC hours ────────────────────────────────────────────────────────
DEAD_ZONE_START_UTC   : int = 0
DEAD_ZONE_END_UTC     : int = 5
BOOM500_START_UTC     : int = 7
BOOM500_END_UTC       : int = 12
CRASH500_START_UTC    : int = 7
CRASH500_END_UTC      : int = 16
BOOM_CRASH_300_START  : int = 7
BOOM_CRASH_300_END    : int = 12
BOOM_CRASH_1000_START : int = 5
BOOM_CRASH_1000_END   : int = 20

# ─── Risk Management ─────────────────────────────────────────────────────────
BASE_STAKE_PCT        : float = 0.01   # 1% of current balance
RISK_PER_TRADE_PCT    : float = 0.01
MIN_STAKE             : float = 0.35
MAX_STAKE             : float = 50.0
MAX_CONCURRENT_TRADES : int   = 10
DAILY_LOSS_LIMIT_PCT  : float = 0.15

# ─── Win-Streak Stake Scaling ─────────────────────────────────────────────────
WIN_STREAK_THRESHOLDS  : List[int]   = [3, 5, 8, 12]
WIN_STREAK_MULTIPLIERS : List[float] = [1.5, 2.0, 3.0, 4.0]
WIN_STREAK_EXTRA_SLOTS : List[int]   = [2, 4, 6, 8]

# ─── Signal quality ───────────────────────────────────────────────────────────
MIN_STRENGTH_RANGE_BREAK : int = 2
MIN_STRENGTH_BOOM_CRASH  : int = 2

# ─── Spike detection ─────────────────────────────────────────────────────────
SPIKE_ATR_MULTIPLIER : float = 3.0
SPIKE_MAX_AGE_BARS   : int   = 2
SPIKE_COOLDOWN_BARS  : int   = 10

# ─── Trade Execution ──────────────────────────────────────────────────────────
TRADE_DURATION      : int = 5
TRADE_DURATION_UNIT : str = "m"

# ─── Scan Cycle ───────────────────────────────────────────────────────────────
SCAN_CYCLE_SLEEP : int = 1

# ─── Symbol Suspension ────────────────────────────────────────────────────────
SYMBOL_WIN_SUSPEND_MINS  : int = 7
SYMBOL_LOSS_SUSPEND_MINS : int = 17
SYMBOL_MIN_GAP_MINUTES   : int = 7
SESSION_BAN_LOSS_THRESHOLD : int = 3

# ─── Contract timeouts ────────────────────────────────────────────────────────
CONTRACT_MAX_AGE_SECONDS           : int = TRADE_DURATION * 60 + 45
CONTRACT_FORCE_CLOSE_AFTER_SECONDS : int = TRADE_DURATION * 60 + 90

# ─── Auto-Redeploy ────────────────────────────────────────────────────────────
REDEPLOY_EVERY_N_CYCLES  : int = 6
RENDER_DEPLOY_HOOK_URL   : str = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")

# ─── Keep-Alive ───────────────────────────────────────────────────────────────
PORT                : int = int(os.environ.get("PORT", 8080))
SELF_URL            : str = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL : int = 40

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL : str = os.environ.get("LOG_LEVEL", "INFO")

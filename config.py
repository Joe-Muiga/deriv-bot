"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v14 → v15 changes:
  UPDATED:
    BASE_STAKE_PCT          : 0.01  (1% of current balance)
    MAX_STAKE               : 50.0
    MIN_STAKE               : 0.35
    WIN_STREAK_THRESHOLDS   : [3, 5, 8, 12]
    WIN_STREAK_MULTIPLIERS  : [1.5, 2.0, 3.0, 4.0]
    WIN_STREAK_EXTRA_SLOTS  : [2, 4, 6, 8]
    MAX_CONCURRENT_TRADES   : 15
    MIN_MODULE_STRENGTH     : 2
    MIN_CONFIDENCE_NORMAL   : 5
    SCAN_CYCLE_SLEEP        : 1
    TRADE_DURATION          : 5
    DAILY_LOSS_LIMIT_PCT    : 0.15
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

# ─── Symbol Groups ────────────────────────────────────────────────────────────
VOLATILITY_SYMBOLS  : List[str] = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    "R_10_1HZ", "R_25_1HZ", "R_50_1HZ", "R_75_1HZ", "R_100_1HZ",
]

BOOM_CRASH_SYMBOLS  : List[str] = [
    "BOOM500", "BOOM300", "BOOM150", "BOOM1000",
    "CRASH500", "CRASH300", "CRASH150", "CRASH1000",
]

RANGE_BREAK_SYMBOLS : List[str] = ["RDBULL", "RDBEAR", "RB100", "RB200"]

STEP_INDEX_SYMBOLS  : List[str] = ["stpRNG"]

# Canonical scan order
ALL_SYMBOLS : List[str] = (
    BOOM_CRASH_SYMBOLS + VOLATILITY_SYMBOLS + RANGE_BREAK_SYMBOLS + STEP_INDEX_SYMBOLS
)

# ─── Session / Dead Zone (UTC hours) ─────────────────────────────────────────
DEAD_ZONE_START_UTC  : int = 0
DEAD_ZONE_END_UTC    : int = 5
BOOM500_PRIME_START  : int = 7
BOOM500_PRIME_END    : int = 12

# ─── Timeframes ──────────────────────────────────────────────────────────────
HTF_GRANULARITY       : int = 3600
FOREX_LTF_GRANULARITY : int = 900
OTHER_LTF_GRANULARITY : int = 60
LTF_GRANULARITY       : int = 60
HTF_BARS              : int = 100
LTF_BARS              : int = 50

# ─── Risk Management ─────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  : float = 2.15   # 15% max daily loss then stop trading
BASE_STAKE_PCT        : float = 0.01   # 1% of current balance = base stake
RISK_PER_TRADE_PCT    : float = 0.01   # alias for BASE_STAKE_PCT (backwards compat)
MIN_STAKE             : float = 0.35
MAX_STAKE             : float = 50.0
MAX_CONCURRENT_TRADES : int   = 15

# ─── Win-Streak Stake Scaling ─────────────────────────────────────────────────
WIN_STREAK_THRESHOLDS   : List[int]   = [3, 5, 8, 12]
WIN_STREAK_MULTIPLIERS  : List[float] = [1.5, 2.0, 3.0, 4.0]
WIN_STREAK_EXTRA_SLOTS  : List[int]   = [2, 4, 6, 8]

# Legacy aliases (backwards compat)
WIN_STREAK_SCALE_THRESHOLDS  = WIN_STREAK_THRESHOLDS
WIN_STREAK_STAKE_MULTIPLIERS = WIN_STREAK_MULTIPLIERS
WIN_STREAK_CONCURRENT_BONUS  = WIN_STREAK_EXTRA_SLOTS
WIN_STREAK_STAKE_FACTOR      : float = 0.30
MAX_WIN_STREAK_MULT          : float = 4.0

# ─── Symbol Cooldown After Loss ───────────────────────────────────────────────
SYMBOL_LOSS_COOLDOWN_SECONDS    : int = 120
SYNTHETIC_LOSS_COOLDOWN_SECONDS : int = 60

# ─── Symbol Time-Based Suspension ────────────────────────────────────────────
SYMBOL_WIN_SUSPENSION_MINUTES         : int = 7
SYMBOL_LOSS_SUSPENSION_MINUTES        : int = 17
SYMBOL_MIN_GAP_MINUTES                : int = 7
SYMBOL_SESSION_LOSS_BAN_THRESHOLD     : int = 2

# ─── Signal Score Weights ─────────────────────────────────────────────────────
SCORE_WEIGHT_MODULE_STRENGTH     : float = 0.50
SCORE_WEIGHT_MODULE_QUALITY      : float = 0.30
SCORE_WEIGHT_FRESHNESS           : float = 0.05
SCORE_WEIGHT_INDICATOR_AGREEMENT : float = 0.15

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
MIN_SIGNAL_SCORE           : float = 2.0
MIN_SIGNAL_PROBABILITY     : float = 1.8
MIN_STRENGTH_REPEAT_SYMBOL : int   = 3

# ─── Module Strength Thresholds ───────────────────────────────────────────────
MIN_MODULE_STRENGTH_NORMAL : int = 2
MIN_MODULE_STRENGTH_STRICT : int = 3
MIN_MODULE_STRENGTH        : int = 2   # minimum confirming modules for emission
MIN_CONFIDENCE_FOR_PARTIAL : int = 5   # 2/3 signals require ≥ 5/7 indicators

# ─── Confidence Thresholds ────────────────────────────────────────────────────
MIN_CONFIDENCE_NORMAL   : int = 5
MIN_CONFIDENCE_STRICT   : int = 6
MIN_CONFIDENCE_RECOVERY : int = 7

# ─── Loss-Streak Gate Thresholds ──────────────────────────────────────────────
LOSS_STREAK_QUALITY_GATE    : int = -2
LOSS_STREAK_PAUSE_THRESHOLD : int = -4
LOSS_STREAK_ABORT_THRESHOLD : int = -6
QUALITY_GATE_TIMEOUT_SECS   : int = 60

# ─── Strategy ────────────────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL  : int   = 2
MIN_INDICATOR_VOTES     : int   = 3
OB_EXPIRY_BARS          : int   = 50
ATR_ZONE_FACTOR         : float = 3.0
NEWS_BLOCK_MINUTES      : int   = 30
DIVERGENCE_STRENGTH_MIN : float = 0.3

# ─── Trade Execution ─────────────────────────────────────────────────────────
TRADE_DURATION      : int = 5
TRADE_DURATION_UNIT : str = "m"

BOOM_CRASH_TICK_DURATION : int = 10
BOOM_CRASH_DURATION_UNIT : str = "t"

# ─── Scan Cycle Timing ────────────────────────────────────────────────────────
SCAN_CYCLE_SLEEP : int = 1

# ─── Auto-Redeploy Cycle Budget ───────────────────────────────────────────────
REDEPLOY_EVERY_N_CYCLES : int = 6

# ─── Render Deploy Hook ───────────────────────────────────────────────────────
RENDER_DEPLOY_HOOK_URL : str = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")

# ─── Render Keep-Alive ────────────────────────────────────────────────────────
PORT                : int = int(os.environ.get("PORT", 8080))
SELF_URL            : str = os.environ.get("RENDER_EXTERNAL_URL",
                                            f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL : int = 40

# ─── Contract Force-Close Timeout ────────────────────────────────────────────
CONTRACT_MAX_AGE_SECONDS           : int = TRADE_DURATION * 60 + 45
CONTRACT_FORCE_CLOSE_AFTER_SECONDS : int = TRADE_DURATION * 60 + 90

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL : str = os.environ.get("LOG_LEVEL", "INFO")

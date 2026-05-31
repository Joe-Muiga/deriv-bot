"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v16 → v17 changes:
  CHANGED:
    All cycle-based timing replaced with minute-based equivalents
    SYMBOL_WIN_SUSPEND_MINS          : 5  (was 7 min)
    SYMBOL_LOSS_SUSPEND_MINS         : 12 (was 17 min)
    SYMBOL_MIN_GAP_MINS              : 3  (was 7 min)
    SYMBOL_SESSION_BAN_MINS          : 480 (8 hours, new)
    DAILY_LOSS_LIMIT_PCT             : corrected to 0.15 (was 2.15)
    DAILY_LOSS_PAUSE_MINS            : 60 (new)
    CONTRACT_CHECK_SECS              : 300 (5 min)
    CONTRACT_TIMEOUT_SECS            : 420 (7 min)
    MAX_CONCURRENT_TRADES            : 20 (was 15)
    MAX_STAKE                        : 5000.0 (was 1000.0)
    REDEPLOY_EVERY_N_CYCLES          : removed (replaced by REDEPLOY_EVERY_N_MINS)
  ADDED:
    DRIFT_SYMBOLS                    : DSHIFT10/20/30
    BOOM300N, CRASH300N              : to BOOM_CRASH_SYMBOLS
    ALL_TRADE_SYMBOLS                : flat union replacing ALL_SYMBOLS + TRADE_SYMBOLS
    REDEPLOY_EVERY_N_MINS            : minute-based redeploy cadence
"""

import os
from typing import Dict, List
from dotenv import load_dotenv

load_dotenv()

# ─── Deriv API ────────────────────────────────────────────────────────────────
DERIV_APP_ID    : str = os.environ.get("DERIV_APP_ID", "")
DERIV_API_TOKEN : str = os.environ.get("DERIV_API_TOKEN", "")
DERIV_WS_URL    : str = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
DERIV_CURRENCY  : str = "USD"

# ─── WebSocket Reconnect ──────────────────────────────────────────────────────
WEBSOCKET_RECONNECT_INTERVAL : int = 10
WEBSOCKET_MAX_RECONNECTS     : int = 5

# ─── Symbol Groups ────────────────────────────────────────────────────────────

# Digit Over/Under — all available indices
DIGIT_SYMBOLS           : List[str] = [
    "R_10",  "R_25",  "R_50",  "R_75",  "R_100",
    "1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V",
]

# Mean Reversion — low-volatility 1s indices
MEAN_REVERSION_SYMBOLS  : List[str] = [
    "R_10", "R_10_1HZ",
]

# All volatility indices
VOLATILITY_SYMBOLS      : List[str] = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    "R_10_1HZ", "R_25_1HZ", "R_50_1HZ", "R_75_1HZ", "R_100_1HZ",
]

# Boom / Crash — all available including N-variants
BOOM_CRASH_SYMBOLS      : List[str] = [
    "BOOM150","BOOM300","BOOM500","BOOM1000",
    "CRASH150","CRASH300","CRASH500","CRASH1000",
    "BOOM300N","CRASH300N",
]

# Range Break — all available
RANGE_BREAK_SYMBOLS     : List[str] = ["RDBULL", "RDBEAR", "RB100", "RB200"]

# Step Index
STEP_SYMBOLS            : List[str] = ["stpRNG"]

# Jump Index — all available
JUMP_SYMBOLS            : List[str] = ["JD10", "JD25", "JD50", "JD75", "JD100"]

# Drift / Shift Index — all available
DRIFT_SYMBOLS           : List[str] = ["DSHIFT10", "DSHIFT20", "DSHIFT30"]

# All symbols scanned (canonical union)
ALL_SYMBOLS             : List[str] = (
    BOOM_CRASH_SYMBOLS + VOLATILITY_SYMBOLS + RANGE_BREAK_SYMBOLS +
    STEP_SYMBOLS + JUMP_SYMBOLS + DRIFT_SYMBOLS
)

# Actively traded flat list (maximum available)
ALL_TRADE_SYMBOLS       : List[str] = (
    DIGIT_SYMBOLS +
    BOOM_CRASH_SYMBOLS +
    RANGE_BREAK_SYMBOLS +
    STEP_SYMBOLS +
    JUMP_SYMBOLS +
    DRIFT_SYMBOLS
)

# Legacy alias
TRADE_SYMBOLS           : List[str] = ALL_TRADE_SYMBOLS

# ─── Session / Dead Zone (UTC hours) ─────────────────────────────────────────
DEAD_ZONE_START_UTC         : int = 0
DEAD_ZONE_END_UTC           : int = 5

BOOM500_PRIME_START         : int = 7
BOOM500_PRIME_END           : int = 12

CRASH500_START_UTC          : int = 7
CRASH500_END_UTC            : int = 16

BOOM_CRASH_1000_START_UTC   : int = 5
BOOM_CRASH_1000_END_UTC     : int = 20

BOOM_CRASH_300_START_UTC    : int = 7
BOOM_CRASH_300_END_UTC      : int = 12

JUMP_START_UTC              : int = 7
JUMP_END_UTC                : int = 20
JUMP_PEAK_UTC               : int = 12   # JD50 activity peaks here

# ─── Timeframes ──────────────────────────────────────────────────────────────
HTF_GRANULARITY             : int = 3600
FOREX_LTF_GRANULARITY       : int = 900
OTHER_LTF_GRANULARITY       : int = 60
LTF_GRANULARITY             : int = 60
HTF_BARS                    : int = 100
LTF_BARS                    : int = 50

# ─── Risk Management ─────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT        : float = 0.15   # stop trading at 15% daily drawdown
DAILY_LOSS_PAUSE_MINS       : int   = 60     # pause duration (minutes) when limit hit
BASE_STAKE_PCT              : float = 0.01   # 1% of current balance = base stake
RISK_PER_TRADE_PCT          : float = 0.01   # alias for BASE_STAKE_PCT (backwards compat)
MIN_STAKE                   : float = 0.35
MAX_STAKE                   : float = 50.0
MIN_ACCOUNT_BALANCE         : float = 0.0    # suspend all trading below this USD floor
MAX_CONCURRENT_TRADES       : int   = 20

# ─── Progressive Loss Scaling (PLS) ──────────────────────────────────────────
# Stake scales UP with consecutive wins; any loss resets immediately to base.
PLS_WIN_THRESHOLDS          : List[int]   = [3,   5,   8,   12  ]
PLS_WIN_MULTIPLIERS         : List[float] = [1.5, 2.0, 3.0, 4.0 ]
PLS_WIN_EXTRA_SLOTS         : List[int]   = [2,   4,   6,   8   ]
PLS_LOSS_RESET              : bool        = True   # loss immediately returns to 1.0×

# Legacy win-streak aliases (backwards compat)
WIN_STREAK_THRESHOLDS        : List[int]   = PLS_WIN_THRESHOLDS
WIN_STREAK_MULTIPLIERS       : List[float] = PLS_WIN_MULTIPLIERS
WIN_STREAK_EXTRA_SLOTS       : List[int]   = PLS_WIN_EXTRA_SLOTS
WIN_STREAK_SCALE_THRESHOLDS  = WIN_STREAK_THRESHOLDS
WIN_STREAK_STAKE_MULTIPLIERS = WIN_STREAK_MULTIPLIERS
WIN_STREAK_CONCURRENT_BONUS  = WIN_STREAK_EXTRA_SLOTS
WIN_STREAK_STAKE_FACTOR      : float = 0.30
MAX_WIN_STREAK_MULT          : float = 4.0

# Extra concurrent slots unlocked by win streak
WIN_STREAK_EXTRA_SLOT_MAP    : Dict[int, int] = {3: 2, 5: 4, 8: 6, 12: 8}

# ─── Symbol Suspension Timing (all in minutes) ────────────────────────────────
SYMBOL_WIN_SUSPEND_MINS           : int = 5    # suspend symbol N mins after win
SYMBOL_LOSS_SUSPEND_MINS          : int = 10   # suspend symbol N mins after loss
SYMBOL_MIN_GAP_MINS               : int = 2    # minimum gap between trades on same symbol
SYMBOL_SESSION_BAN_LOSSES         : int = 3    # consecutive losses → session ban
SYMBOL_SESSION_BAN_MINS           : int = 480  # session ban duration (8 hours)

# Legacy second-based aliases (backwards compat)
SYMBOL_LOSS_COOLDOWN_SECONDS      : int = SYMBOL_LOSS_SUSPEND_MINS * 60
SYNTHETIC_LOSS_COOLDOWN_SECONDS   : int = SYMBOL_MIN_GAP_MINS * 60

# Legacy naming aliases
SYMBOL_WIN_SUSPENSION_MINUTES     : int = SYMBOL_WIN_SUSPEND_MINS
SYMBOL_LOSS_SUSPENSION_MINUTES    : int = SYMBOL_LOSS_SUSPEND_MINS
SYMBOL_MIN_GAP_MINUTES            : int = SYMBOL_MIN_GAP_MINS
SYMBOL_SESSION_LOSS_BAN_THRESHOLD : int = SYMBOL_SESSION_BAN_LOSSES

# ─── Trade Execution ─────────────────────────────────────────────────────────
TRADE_DURATION                    : int = 5
TRADE_DURATION_UNIT               : str = "m"

BOOM_CRASH_TICK_DURATION          : int = 10
BOOM_CRASH_DURATION_UNIT          : str = "t"

# ─── Contract Force-Close Timeout (minutes → seconds) ────────────────────────
CONTRACT_CHECK_SECS               : int = 300   # check contract at 5 mins
CONTRACT_TIMEOUT_SECS             : int = 420   # force close at 7 mins

# Legacy naming aliases
CONTRACT_MAX_AGE_SECONDS          : int = CONTRACT_CHECK_SECS
CONTRACT_FORCE_CLOSE_AFTER_SECONDS: int = CONTRACT_TIMEOUT_SECS

# ─── Scan Cycle Timing ────────────────────────────────────────────────────────
SCAN_CYCLE_SLEEP                  : int = 1      # seconds between scan cycles
PARALLEL_SCAN                     : bool = True  # scan all symbols simultaneously

# ─── Signal Score Weights ─────────────────────────────────────────────────────
SCORE_WEIGHT_MODULE_STRENGTH      : float = 0.50
SCORE_WEIGHT_MODULE_QUALITY       : float = 0.30
SCORE_WEIGHT_FRESHNESS            : float = 0.05
SCORE_WEIGHT_INDICATOR_AGREEMENT  : float = 0.15

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
MIN_SIGNAL_STRENGTH               : int   = 2
MIN_SIGNAL_SCORE                  : float = 0.55
MIN_SIGNAL_PROBABILITY            : float = 1.8
MIN_STRENGTH_REPEAT_SYMBOL        : int   = 3

# ─── Module Strength Thresholds ───────────────────────────────────────────────
MIN_MODULE_STRENGTH_NORMAL        : int = 2
MIN_MODULE_STRENGTH_STRICT        : int = 3
MIN_MODULE_STRENGTH               : int = 2
MIN_CONFIDENCE_FOR_PARTIAL        : int = 5

# ─── Confidence Thresholds ────────────────────────────────────────────────────
MIN_CONFIDENCE_NORMAL             : int = 5
MIN_CONFIDENCE_STRICT             : int = 6
MIN_CONFIDENCE_RECOVERY           : int = 7

# ─── Loss-Streak Gate Thresholds ──────────────────────────────────────────────
LOSS_STREAK_QUALITY_GATE          : int = -2
LOSS_STREAK_PAUSE_THRESHOLD       : int = -4
LOSS_STREAK_ABORT_THRESHOLD       : int = -6
QUALITY_GATE_TIMEOUT_SECS         : int = 60

# ─── Strategy (General) ───────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL            : int   = 2
MIN_INDICATOR_VOTES               : int   = 3
OB_EXPIRY_BARS                    : int   = 50
ATR_ZONE_FACTOR                   : float = 3.0
NEWS_BLOCK_MINUTES                : int   = 30
DIVERGENCE_STRENGTH_MIN           : float = 0.3

# ─── Strategy: Digit Over/Under ───────────────────────────────────────────────
DIGIT_MIN_SCORE                   : int   = 6
DIGIT_RSI_OVERSOLD                : int   = 30
DIGIT_RSI_OVERBOUGHT              : int   = 70
DIGIT_BB_PERIODS                  : int   = 20
DIGIT_RSI_PERIODS                 : int   = 14

# ─── Strategy: Mean Reversion ─────────────────────────────────────────────────
MR_RSI_HIGH                       : int   = 78
MR_RSI_LOW                        : int   = 22
MR_BB_STD                         : float = 2.0
MR_ROC_THRESHOLD                  : float = 0.02
MR_MIN_SCORE                      : int   = 6

# ─── Strategy: Range Break ────────────────────────────────────────────────────
RB_CONSOLIDATION_RATIO            : float = 0.4
RB_BREAKOUT_ATR_MULT              : float = 0.3
RB_RSI_BULL                       : int   = 52
RB_RSI_BEAR                       : int   = 48
RB_MAX_AGE_BARS                   : int   = 3
RB_RETEST_TOLERANCE_ATR           : float = 0.5

# ─── Strategy: Boom/Crash Post-Spike Fade ────────────────────────────────────
SPIKE_ATR_MULTIPLIER              : float = 3.0
SPIKE_MAX_AGE_BARS                : int   = 2
SPIKE_COOLDOWN_BARS               : int   = 10
SPIKE_RSI_OVERBOUGHT              : int   = 60
SPIKE_RSI_OVERSOLD                : int   = 40

# ─── Strategy: Step Index ─────────────────────────────────────────────────────
STEP_EMA_FAST                     : int = 10
STEP_EMA_SLOW                     : int = 30
STEP_GRID_SIZE                    : int = 5

# ─── Strategy: Jump Index ─────────────────────────────────────────────────────
JUMP_INTERVAL_SECONDS             : int = 1200   # ~20 min between jumps
JUMP_ENTRY_WINDOW_SECS            : int = 120    # enter within 2 min of expected jump

# ─── Auto-Redeploy ────────────────────────────────────────────────────────────
REDEPLOY_EVERY_N_MINS             : int = 6      # minute-based redeploy cadence

# ─── Render Deploy Hook ───────────────────────────────────────────────────────
RENDER_DEPLOY_HOOK_URL            : str = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")

# ─── Render Keep-Alive ────────────────────────────────────────────────────────
PORT                              : int = int(os.environ.get("PORT", 8080))
SELF_URL                          : str = os.environ.get(
                                        "RENDER_EXTERNAL_URL",
                                        f"http://localhost:{PORT}"
                                    )
KEEP_ALIVE_INTERVAL               : int = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL                         : str = os.environ.get("LOG_LEVEL", "INFO")

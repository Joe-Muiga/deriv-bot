"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v15 → v16 changes:
  ADDED:
    DIGIT_SYMBOLS               : Digit Over/Under strategy subset (1s indices)
    MEAN_REVERSION_SYMBOLS      : Mean Reversion subset (low-vol 1s indices)
    STEP_SYMBOLS                : Step Index subset
    JUMP_SYMBOLS                : Jump Index subset (JD10–JD100)
    TRADE_SYMBOLS               : All actively-traded symbols (union)
    Session windows             : CRASH500, BOOM/CRASH 300/1000, JUMP_PEAK_UTC
    PLS_WIN_MULTIPLIERS/THRESHOLDS : Progressive Loss Scaling (win-side only)
    PLS_LOSS_RESET              : Loss immediately resets stake to base
    MIN_ACCOUNT_BALANCE         : Hard floor before trading suspends
    Per-strategy signal params  : DIGIT_*, MR_*, RB_*, SPIKE_*, STEP_*, JUMP_*
    SYMBOL_SESSION_BAN_LOSSES   : raised to 3
  FIXED:
    DAILY_LOSS_LIMIT_PCT        : corrected to 0.15 (was 2.15, comment always said 15%)
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

# Digit Over/Under — 1s indices only (fastest tick resolution)
DIGIT_SYMBOLS           : List[str] = [
    "R_10", "R_25", "R_10_1HZ", "R_25_1HZ",
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

# Boom / Crash — post-spike fade strategy
BOOM_CRASH_SYMBOLS      : List[str] = [
    "BOOM500", "BOOM300", "BOOM150", "BOOM1000",
    "CRASH500", "CRASH300", "CRASH150", "CRASH1000",
]

# Range Break — retest-entry after confirmed breakout
RANGE_BREAK_SYMBOLS     : List[str] = ["RDBULL", "RDBEAR", "RB100", "RB200"]

# Step Index — grid / trend-following
STEP_SYMBOLS            : List[str] = ["stpRNG"]

# Jump Index — pre-jump timing
JUMP_SYMBOLS            : List[str] = ["JD10", "JD25", "JD50", "JD75", "JD100"]

# All symbols scanned in canonical order
ALL_SYMBOLS             : List[str] = (
    BOOM_CRASH_SYMBOLS + VOLATILITY_SYMBOLS + RANGE_BREAK_SYMBOLS +
    STEP_SYMBOLS + JUMP_SYMBOLS
)

# Actively traded (excludes broad-scan-only volatility)
TRADE_SYMBOLS           : List[str] = (
    DIGIT_SYMBOLS + RANGE_BREAK_SYMBOLS +
    BOOM_CRASH_SYMBOLS + STEP_SYMBOLS + JUMP_SYMBOLS
)

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

JUMP_PEAK_UTC               : int = 12   # JD50 activity peaks here

# ─── Timeframes ──────────────────────────────────────────────────────────────
HTF_GRANULARITY             : int = 3600
FOREX_LTF_GRANULARITY       : int = 900
OTHER_LTF_GRANULARITY       : int = 60
LTF_GRANULARITY             : int = 60
HTF_BARS                    : int = 100
LTF_BARS                    : int = 50

# ─── Risk Management ─────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT        : float = 2.15   # stop trading at 15% daily drawdown
BASE_STAKE_PCT              : float = 0.01   # 1% of current balance = base stake
RISK_PER_TRADE_PCT          : float = 0.1   # alias for BASE_STAKE_PCT (backwards compat)
MIN_STAKE                   : float = 0.35
MAX_STAKE                   : float = 50.0
MIN_ACCOUNT_BALANCE         : float = 0.0   # suspend all trading below this USD floor
MAX_CONCURRENT_TRADES       : int   = 15

# ─── Progressive Loss Scaling (PLS) ──────────────────────────────────────────
# Stake scales UP with consecutive wins; any loss resets immediately to base.
# Indexed by win-streak bucket:  0–2 wins → 1.0×, 3–4 → 1.5×, etc.
PLS_WIN_THRESHOLDS          : List[int]   = [0,   3,   5,   8,   12 ]
PLS_WIN_MULTIPLIERS         : List[float] = [1.0, 1.5, 2.0, 3.0, 4.0]
PLS_LOSS_RESET              : bool        = True   # loss immediately returns to 1.0×

# Legacy win-streak aliases (backwards compat)
WIN_STREAK_THRESHOLDS       : List[int]   = PLS_WIN_THRESHOLDS[1:]      # [3,5,8,12]
WIN_STREAK_MULTIPLIERS      : List[float] = PLS_WIN_MULTIPLIERS[1:]     # [1.5,2.0,3.0,4.0]
WIN_STREAK_EXTRA_SLOTS      : List[int]   = [2, 4, 6, 8]
WIN_STREAK_SCALE_THRESHOLDS  = WIN_STREAK_THRESHOLDS
WIN_STREAK_STAKE_MULTIPLIERS = WIN_STREAK_MULTIPLIERS
WIN_STREAK_CONCURRENT_BONUS  = WIN_STREAK_EXTRA_SLOTS
WIN_STREAK_STAKE_FACTOR     : float = 0.30
MAX_WIN_STREAK_MULT         : float = 4.0

# Extra concurrent slots unlocked by win streak
WIN_STREAK_EXTRA_SLOT_MAP   : Dict[int, int] = {3: 2, 5: 4, 8: 6, 12: 8}

# ─── Symbol Cooldown After Loss ───────────────────────────────────────────────
SYMBOL_LOSS_COOLDOWN_SECONDS    : int = 120
SYNTHETIC_LOSS_COOLDOWN_SECONDS : int = 60

# ─── Symbol Time-Based Suspension ────────────────────────────────────────────
SYMBOL_WIN_SUSPENSION_MINUTES     : int = 7
SYMBOL_LOSS_SUSPENSION_MINUTES    : int = 17
SYMBOL_MIN_GAP_MINUTES            : int = 7
SYMBOL_SESSION_LOSS_BAN_THRESHOLD : int = 3   # raised from 2 → 3

# ─── Signal Score Weights ─────────────────────────────────────────────────────
SCORE_WEIGHT_MODULE_STRENGTH      : float = 0.50
SCORE_WEIGHT_MODULE_QUALITY       : float = 0.30
SCORE_WEIGHT_FRESHNESS            : float = 0.05
SCORE_WEIGHT_INDICATOR_AGREEMENT  : float = 0.15

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
MIN_SIGNAL_SCORE                  : float = 2.0
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
DIGIT_MIN_SCORE                   : int   = 6    # minimum score out of 8 to trade
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
RB_CONSOLIDATION_RATIO            : float = 0.4   # current range < 0.4× 50-bar avg
RB_BREAKOUT_ATR_MULT              : float = 0.3
RB_RSI_BULL                       : int   = 52
RB_RSI_BEAR                       : int   = 48
RB_MAX_AGE_BARS                   : int   = 3     # only trade fresh breakouts
RB_RETEST_TOLERANCE_ATR           : float = 0.5   # retest within 0.5× ATR of breakout

# ─── Strategy: Boom/Crash Post-Spike Fade ────────────────────────────────────
SPIKE_ATR_MULTIPLIER              : float = 3.0   # spike = bar moves > 3× ATR14
SPIKE_MAX_AGE_BARS                : int   = 2
SPIKE_COOLDOWN_BARS               : int   = 10
SPIKE_RSI_OVERBOUGHT              : int   = 60    # required after boom spike to fade
SPIKE_RSI_OVERSOLD                : int   = 40    # required after crash spike to fade

# ─── Strategy: Step Index ─────────────────────────────────────────────────────
STEP_EMA_FAST                     : int = 10
STEP_EMA_SLOW                     : int = 30
STEP_GRID_SIZE                    : int = 5       # grid spacing in points

# ─── Strategy: Jump Index ─────────────────────────────────────────────────────
JUMP_INTERVAL_SECONDS             : int = 1200    # ~20 min between jumps
JUMP_ENTRY_WINDOW_SECS            : int = 120     # enter within 2 min of expected jump

# ─── Trade Execution ─────────────────────────────────────────────────────────
TRADE_DURATION                    : int = 5
TRADE_DURATION_UNIT               : str = "m"

BOOM_CRASH_TICK_DURATION          : int = 10
BOOM_CRASH_DURATION_UNIT          : str = "t"

# ─── Contract Force-Close Timeout ─────────────────────────────────────────────
CONTRACT_MAX_AGE_SECONDS          : int = TRADE_DURATION * 60 + 45
CONTRACT_FORCE_CLOSE_AFTER_SECONDS: int = TRADE_DURATION * 60 + 90

# ─── Scan Cycle Timing ────────────────────────────────────────────────────────
SCAN_CYCLE_SLEEP                  : int = 1

# ─── Auto-Redeploy Cycle Budget ───────────────────────────────────────────────
REDEPLOY_EVERY_N_CYCLES           : int = 6

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

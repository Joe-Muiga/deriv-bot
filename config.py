import os

# ── GENERAL ───────────────────────────────────────────────────
LOG_LEVEL = "INFO"
DEBUG     = False
VERSION   = "1.0.0"

# ── DERIV API ─────────────────────────────────────────────────
DERIV_API_TOKEN = os.environ.get("DERIV_API_TOKEN", "")
DERIV_APP_ID    = os.environ.get("DERIV_APP_ID", "1089")

# ── SERVER ────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 10000))
SELF_URL = os.environ.get("SELF_URL", os.environ.get("RENDER_EXTERNAL_URL", ""))
KEEP_ALIVE_INTERVAL = 600  # seconds between self-ping requests

# ── ALL DERIV SYNTHETIC INDICES ──────────────────────────────

# Standard Volatility (2s tick)
VOLATILITY_STANDARD = [
    "R_10","R_25","R_50","R_75","R_100",
]

# 1-Second Volatility (faster tick)
VOLATILITY_1S = [
    "1HZ10V","1HZ25V","1HZ50V",
    "1HZ75V","1HZ100V","1HZ150V",
    "1HZ200V","1HZ250V",
]

# Boom & Crash
# NOTE: Removed from trading — Boom/Crash symbols do NOT support
# CALL/PUT Rise/Fall contracts via the API (OfferingsValidationError).
# They require different contract types (e.g. multipliers) not used here.
BOOM_CRASH = []

# Step Index
STEP = ["stpRNG"]

# Jump Indices
# NOTE: Removed from trading — Jump indices do NOT support
# CALL/PUT Rise/Fall contracts via the API (OfferingsValidationError).
JUMP = []

# Range Break
RANGE_BREAK = ["RDBULL","RDBEAR"]

# Drift Switch
DRIFT = ["DSHIFT10","DSHIFT20","DSHIFT30"]

# Only these symbols support CALL/PUT Rise/Fall via Deriv API
RISE_FALL_SYMBOLS = [
    "R_10","R_25","R_50","R_75","R_100",
    "1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V",
    "RDBULL","RDBEAR","stpRNG",
]

# Digit (Match/Differ/Over/Under/Even/Odd) contracts are not used by this bot —
# only CALL/PUT Rise/Fall is traded — so this stays empty. signal_engine.py
# checks `if symbol in config.DIGIT_SYMBOLS` to route digit-specific logic;
# an empty list means that branch is always skipped, as intended.
DIGIT_SYMBOLS = []

# ── STRATEGY ROUTING (signal_engine.py) ──────────────────────
# Every traded symbol is routed to exactly one strategy evaluator.
MEAN_REVERSION_SYMBOLS = VOLATILITY_STANDARD + VOLATILITY_1S  # all 10 vol indices
RANGE_BREAK_SYMBOLS    = RANGE_BREAK                            # RDBULL, RDBEAR
BOOM_CRASH_SYMBOLS     = BOOM_CRASH                              # empty — not traded
STEP_SYMBOLS           = STEP                                    # stpRNG

# All symbols combined — Rise/Fall compatible only
ALL_SYMBOLS       = RISE_FALL_SYMBOLS
ALL_TRADE_SYMBOLS = RISE_FALL_SYMBOLS
VOLATILITY_SYMBOLS = RISE_FALL_SYMBOLS  # alias for compatibility with bot_engine.py

# ── MULTIPLIER SETTINGS ──────────────────────────────────────
# Higher volatility = higher multiplier potential
MULTIPLIER_MAP = {
    # Low volatility — moderate multiplier
    "R_10":    100, "1HZ10V":  100,
    "R_25":    200, "1HZ25V":  200,
    # Medium volatility
    "R_50":    300, "1HZ50V":  300,
    "R_75":    500, "1HZ75V":  500,
    # High volatility — maximum multiplier
    "R_100":   500, "1HZ100V": 500,
    "1HZ150V": 500, "1HZ200V": 500,
    "1HZ250V": 500,
    # Boom/Crash — moderate (spike risk)
    "BOOM150": 100, "BOOM300": 100,
    "BOOM500": 100, "BOOM1000":100,
    "CRASH150":100, "CRASH300":100,
    "CRASH500":100, "CRASH1000":100,
    # Others
    "stpRNG":  200,
    "JD10":    100, "JD25":    200,
    "JD50":    300, "JD75":    400,
    "JD100":   500,
    "RDBULL":  200, "RDBEAR":  200,
    "DSHIFT10":200, "DSHIFT20":200,
    "DSHIFT30":200,
}
DEFAULT_MULTIPLIER = 100

# ── STOP LOSS AND TAKE PROFIT (% of stake) ──────────────────
# Stop loss as % of stake — caps maximum loss per trade
STOP_LOSS_MAP = {
    # Low vol — tighter SL
    "R_10":    30.0,  "1HZ10V":  30.0,
    "R_25":    40.0,  "1HZ25V":  40.0,
    # Medium vol
    "R_50":    50.0,  "1HZ50V":  50.0,
    "R_75":    60.0,  "1HZ75V":  60.0,
    # High vol — wider SL to avoid noise
    "R_100":   70.0,  "1HZ100V": 70.0,
    "1HZ150V": 75.0,  "1HZ200V": 80.0,
    "1HZ250V": 80.0,
    # Boom/Crash — wide SL due to spikes
    "BOOM150": 50.0,  "BOOM300": 50.0,
    "BOOM500": 50.0,  "BOOM1000":50.0,
    "CRASH150":50.0,  "CRASH300":50.0,
    "CRASH500":50.0,  "CRASH1000":50.0,
}
DEFAULT_STOP_LOSS_PCT = 50.0

# Take profit = 2x stop loss (2:1 RR minimum)
TAKE_PROFIT_RATIO = 2.0

# ── STAKE SETTINGS ───────────────────────────────────────────
BASE_STAKE_PCT       = 0.005   # 0.5% per trade
MIN_STAKE            = 0.35
MAX_STAKE            = 0.35
DAILY_LOSS_LIMIT_PCT = 0.15   # 20% max daily loss
DAILY_LOSS_PAUSE_MINS = 30

# ── AGGRESSIVE COMPOUNDING ───────────────────────────────────
PLS_WIN_THRESHOLDS  = [3,   5,   8,   12,  15  ]
PLS_WIN_MULTIPLIERS = [2.0, 3.0, 5.0, 8.0, 10.0]
PLS_WIN_EXTRA_SLOTS = [3,   6,   9,   12,  15  ]

# ── CONCURRENT TRADES ────────────────────────────────────────
MAX_CONCURRENT_TRADES = 30

# ── TIMEFRAMES ───────────────────────────────────────────────
HTF_GRANULARITY   = 3600   # 1H
MTF_GRANULARITY   = 300    # 5M
LTF_GRANULARITY   = 60     # 1M
HTF_BARS          = 100
MTF_BARS          = 50
LTF_BARS          = 30

# ── SIGNAL SETTINGS ──────────────────────────────────────────
MIN_SIGNAL_SCORE       = 0.68
MIN_STRATEGY_AGREEMENT = 4

# SMC parameters
OB_LOOKBACK            = 50
FVG_MIN_ATR            = 0.5
SWEEP_LOOKBACK         = 20
SWING_LOOKBACK         = 5
FIB_LEVELS             = [0.382, 0.5, 0.618, 0.786]
FIB_TOLERANCE          = 0.1
EMA_FAST              = 8
EMA_SLOW              = 21
EMA_TREND             = 50
RSI_PERIOD            = 14
RSI_OVERBOUGHT        = 70
RSI_OVERSOLD          = 30
MOMENTUM_LOOKBACK     = 10
ATR_PERIOD            = 14
BREAKOUT_ATR_MULT     = 1.5

# ── CONTRACT SETTINGS ────────────────────────────────────────
# Multiplier contracts — keep short to avoid funding fees
MAX_TRADE_OPEN_MINS   = 30   # force close at 30 min
CHECK_TRADE_MINS      = 20   # check at 20 min
CONTRACT_CHECK_SECS   = 1200
CONTRACT_TIMEOUT_SECS = 1800

# ── SYMBOL SUSPENSION (minutes) ──────────────────────────────
SYMBOL_WIN_SUSPEND_MINS   = 20
SYMBOL_LOSS_SUSPEND_MINS  = 55
SYMBOL_MIN_GAP_MINS       = 1
SYMBOL_SESSION_BAN_LOSSES = 4

# ── SCANNING ────────────────────────────────────────────────
SCAN_CYCLE_SLEEP       = 1
INIT_BATCH_SIZE        = 8
INIT_BATCH_DELAY       = 0.3
PRIORITY_SYMBOLS = [
    "R_75","R_100","1HZ75V","1HZ100V",
    "1HZ250V","1HZ150V","R_50","R_25",
    "RDBULL","RDBEAR"
]

# ── RATE LIMITING ────────────────────────────────────────────
BUY_REQUEST_DELAY_SECS = 3.0
MAX_BUY_PER_SECOND     = 3

# ── RENDER ──────────────────────────────────────────────────
RENDER_DEPLOY_HOOK_URL = os.environ.get(
    "RENDER_DEPLOY_HOOK_URL","")
REDEPLOY_EVERY_N_CYCLES = 8

# ── ALIASES (required by bot_engine.py / risk_manager.py) ────
RISK_PER_TRADE_PCT = BASE_STAKE_PCT          # alias
MAX_CONCURRENT     = 20               # alias
DAILY_LOSS_LIMIT   = DAILY_LOSS_LIMIT_PCT    # alias

# ── ADDITIONAL SIGNAL/RISK SETTINGS ──────────────────────────
MIN_MODULES_FOR_SIGNAL     = 3
MIN_INDICATOR_VOTES        = 3
OB_EXPIRY_BARS             = 100
NEWS_BLOCK_MINUTES         = 60
FOREX_LTF_GRANULARITY      = 900
OTHER_LTF_GRANULARITY      = 60
MIN_SIGNAL_PROBABILITY     = 1.8
MIN_STRENGTH_REPEAT_SYMBOL = 3

# ── DERIV WEBSOCKET ───────────────────────────────────────────
DERIV_WS_URL : str = os.environ.get(
    "DERIV_WS_URL",
    f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
)

# ── SIGNAL GENERATION GATES (additional) ─────────────────────
MIN_SCORE                  = 2.0
MIN_CONFLUENCE             = 2
MIN_MODULE_STRENGTH        = 2
MIN_MODULE_STRENGTH_NORMAL = 2
MIN_CONFIDENCE_NORMAL      = 5
MIN_CONFIDENCE_FOR_PARTIAL = 5

# ── SESSION TIMING — disabled for 24/7 synthetics ────────────
DEAD_ZONE_START_UTC  = 0
DEAD_ZONE_END_UTC    = 5
BOOM500_PRIME_START  = 7
BOOM500_PRIME_END    = 12
TRADE_DURATION = 14
TRADE_DURATION_UNIT = "m"

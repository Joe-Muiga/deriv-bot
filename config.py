import os

# ── STRATEGY SYMBOLS ─────────────────────────────────────────
# Digit Over/Under — 1s indices only
DIGIT_SYMBOLS = [
    "R_10", "R_25", "R_10_1HZ", "R_25_1HZ"
]

# Mean reversion — low volatility 1s indices
MEAN_REVERSION_SYMBOLS = [
    "R_10", "R_10_1HZ"
]

# Range Break — retest entry after confirmed breakout
RANGE_BREAK_SYMBOLS = [
    "RDBULL", "RDBEAR"
]

# Boom/Crash — post-spike fade only
BOOM_CRASH_SYMBOLS = [
    "BOOM500", "BOOM300", "BOOM1000",
    "CRASH500", "CRASH300", "CRASH1000"
]

# Step Index — grid/trend following
STEP_SYMBOLS = ["stpRNG"]

# Jump Index — pre-jump timing
JUMP_SYMBOLS = ["JD10", "JD25", "JD50", "JD75", "JD100"]

# All tradeable symbols
TRADE_SYMBOLS = (
    DIGIT_SYMBOLS + RANGE_BREAK_SYMBOLS +
    BOOM_CRASH_SYMBOLS + STEP_SYMBOLS + JUMP_SYMBOLS
)

# ── SESSION TIMING (UTC) ──────────────────────────────────────
DEAD_ZONE_START_UTC     = 0
DEAD_ZONE_END_UTC       = 5
BOOM500_START_UTC       = 7
BOOM500_END_UTC         = 12
CRASH500_START_UTC      = 7
CRASH500_END_UTC        = 16
BOOM_CRASH_1000_START   = 5
BOOM_CRASH_1000_END     = 20
BOOM_CRASH_300_START    = 7
BOOM_CRASH_300_END      = 12
JUMP_PEAK_UTC           = 12   # Jump 50 peaks here

# ── STAKE & RISK ──────────────────────────────────────────────
BASE_STAKE_PCT          = 0.01   # 1% of current balance
MIN_STAKE               = 0.35
MAX_STAKE               = 50.0
MIN_ACCOUNT_BALANCE     = 50.0
DAILY_LOSS_LIMIT_PCT    = 0.15   # stop at 15% daily loss

# ── PLS STAKE SCALING (Progressive Loss Scaling) ─────────────
# Increases gradually on wins — never doubles on losses
PLS_WIN_MULTIPLIERS     = [1.0, 1.5, 2.0, 3.0, 4.0]
PLS_WIN_THRESHOLDS      = [0,   3,   5,   8,   12 ]
PLS_LOSS_RESET          = True   # any loss resets to base immediately

# ── CONCURRENT TRADES ─────────────────────────────────────────
MAX_CONCURRENT_TRADES   = 15
WIN_STREAK_EXTRA_SLOTS  = {3: 2, 5: 4, 8: 6, 12: 8}

# ── SIGNAL THRESHOLDS ─────────────────────────────────────────
# Digit Over/Under
DIGIT_MIN_SCORE         = 6      # minimum score out of 8 to trade
DIGIT_RSI_OVERSOLD      = 30
DIGIT_RSI_OVERBOUGHT    = 70
DIGIT_BB_PERIODS        = 20
DIGIT_RSI_PERIODS       = 14

# Mean Reversion
MR_RSI_HIGH             = 78
MR_RSI_LOW              = 22
MR_BB_STD               = 2.0
MR_ROC_THRESHOLD        = 0.02
MR_MIN_SCORE            = 6      # documented threshold from research

# Range Break
RB_CONSOLIDATION_RATIO  = 0.4    # current range < 0.4x 50-bar average
RB_BREAKOUT_ATR_MULT    = 0.3
RB_RSI_BULL             = 52
RB_RSI_BEAR             = 48
RB_MAX_AGE_BARS         = 3      # only fresh breakouts
RB_RETEST_TOLERANCE_ATR = 0.5   # retest must come within 0.5x ATR of breakout level

# Boom/Crash post-spike fade
SPIKE_ATR_MULTIPLIER    = 3.0   # spike = bar moves > 3x ATR14
SPIKE_MAX_AGE_BARS      = 2
SPIKE_COOLDOWN_BARS     = 10
SPIKE_RSI_OVERBOUGHT    = 60    # required after boom spike before fading
SPIKE_RSI_OVERSOLD      = 40    # required after crash spike before fading

# Step Index
STEP_EMA_FAST           = 10
STEP_EMA_SLOW           = 30
STEP_GRID_SIZE          = 5     # grid spacing in points

# Jump Index
JUMP_INTERVAL_SECONDS   = 1200  # ~20 min between jumps
JUMP_ENTRY_WINDOW_SECS  = 120   # enter within 2 min before expected jump

# ── SCANNING ─────────────────────────────────────────────────
SCAN_CYCLE_SLEEP        = 1
LTF_BARS                = 50
HTF_BARS                = 100
LTF_GRANULARITY         = 60
HTF_GRANULARITY         = 3600

# ── CONTRACT ──────────────────────────────────────────────────
TRADE_DURATION          = 5
TRADE_DURATION_UNIT     = "m"
CONTRACT_MAX_AGE_SECS   = TRADE_DURATION * 60 + 45
CONTRACT_FORCE_CLOSE_SECS = TRADE_DURATION * 60 + 90

# ── SYMBOL SUSPENSION ─────────────────────────────────────────
SYMBOL_WIN_SUSPEND_MINS  = 7
SYMBOL_LOSS_SUSPEND_MINS = 17
SYMBOL_MIN_GAP_MINS      = 7
SYMBOL_SESSION_BAN_LOSSES = 3

# ── RENDER ────────────────────────────────────────────────────
RENDER_DEPLOY_HOOK_URL  = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")
REDEPLOY_EVERY_N_CYCLES = 6

"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v10 → v11 changes (Change 5):

  NEW — Win-streak scaling constants:
    WIN_STREAK_SCALE_THRESHOLDS    : streak thresholds that trigger scaling
    WIN_STREAK_STAKE_MULTIPLIERS   : stake multipliers at each threshold
    WIN_STREAK_CONCURRENT_BONUS    : extra concurrent slots at each threshold

  NEW — Tiered loss-streak gate constants:
    LOSS_STREAK_PAUSE_THRESHOLD    : streak ≤ this → 1-cycle pause + strength=3
    LOSS_STREAK_ABORT_THRESHOLD    : streak ≤ this → 3-cycle pause + strength=3 + conf≥7

  NEW — Confidence gate constants (count of M3 indicators agreeing):
    MIN_CONFIDENCE_NORMAL          : required for normal trading
    MIN_CONFIDENCE_STRICT          : required when streak ≤ LOSS_STREAK_QUALITY_GATE
    MIN_CONFIDENCE_RECOVERY        : required when streak ≤ LOSS_STREAK_ABORT_THRESHOLD

  NEW — Module strength constants:
    MIN_MODULE_STRENGTH_NORMAL     : min modules confirming under normal conditions
    MIN_MODULE_STRENGTH_STRICT     : min modules confirming under quality gate

  NEW — Signal score threshold:
    MIN_SIGNAL_SCORE               : replaces / supplements MIN_SIGNAL_PROBABILITY
                                     for the new 3-component score formula

  CHANGED — TRADE_DURATION minimum enforced at 5 minutes (unchanged value, now doc'd).

  All v10 values (cooldowns, ATR_ZONE_FACTOR, etc.) preserved unchanged.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Deriv API ────────────────────────────────────────────────────────────────
DERIV_APP_ID    = os.environ.get("DERIV_APP_ID", "1089")
DERIV_API_TOKEN = os.environ.get("DERIV_API_TOKEN", "")
DERIV_WS_URL    = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
DERIV_CURRENCY  = "USD"

# ─── Timeframes ──────────────────────────────────────────────────────────────
HTF_GRANULARITY       = 3600
FOREX_LTF_GRANULARITY = 900
OTHER_LTF_GRANULARITY = 60
LTF_GRANULARITY       = 60
HTF_BARS              = 100
LTF_BARS              = 200

# ─── Risk Management ─────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  = 0.90
RISK_PER_TRADE_PCT    = 0.01
MIN_STAKE             = 0.35
MAX_STAKE             = 500.0
MAX_CONCURRENT_TRADES = 20

# ─── Win-Streak Stake Scaling ─────────────────────────────────────────────────
# Each element corresponds: streak ≥ threshold[i] → multiplier[i] × base stake
WIN_STREAK_SCALE_THRESHOLDS  = [3, 4, 6, 8]          # streak levels
WIN_STREAK_STAKE_MULTIPLIERS = [1.5, 2.0, 3.0, 4.0]  # stake multiplier at level
WIN_STREAK_CONCURRENT_BONUS  = [0,   2,   4,   6]    # extra concurrent slots

# Legacy fields (still used in some places — kept for backward compat)
WIN_STREAK_STAKE_FACTOR = 0.30
MAX_WIN_STREAK_MULT     = 4.0

# ─── Symbol Cooldown After Loss ───────────────────────────────────────────────
SYMBOL_LOSS_COOLDOWN_SECONDS     = 120
SYNTHETIC_LOSS_COOLDOWN_SECONDS  = 60

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
# New 3-component score threshold (module strength 40% + confidence 35% + freshness 25%)
MIN_SIGNAL_SCORE           = 2.0   # minimum composite score to pass to execution

# Legacy threshold — kept for fallback in bot_engine if MIN_SIGNAL_SCORE absent
MIN_SIGNAL_PROBABILITY     = 1.8

MIN_STRENGTH_REPEAT_SYMBOL = 3     # full 3/3 required for Round-2 repeat-symbol trades

# ─── Module Strength Thresholds ───────────────────────────────────────────────
MIN_MODULE_STRENGTH_NORMAL = 2    # minimum confirming modules under normal conditions
MIN_MODULE_STRENGTH_STRICT = 3    # minimum confirming modules under quality gate

# ─── Confidence Thresholds (M3 indicator agreement out of 7) ─────────────────
MIN_CONFIDENCE_NORMAL   = 5    # normal trading conditions
MIN_CONFIDENCE_STRICT   = 6    # streak ≤ LOSS_STREAK_QUALITY_GATE (tier-2/4)
MIN_CONFIDENCE_RECOVERY = 7    # streak ≤ LOSS_STREAK_ABORT_THRESHOLD (tier-6)

# ─── Loss-Streak Gate Thresholds ──────────────────────────────────────────────
LOSS_STREAK_QUALITY_GATE      = -2   # tier-2: strength=3 required
LOSS_STREAK_PAUSE_THRESHOLD   = -4   # tier-4: 1-cycle pause + strength=3
LOSS_STREAK_ABORT_THRESHOLD   = -6   # tier-6: 3-cycle pause + strength=3 + confidence≥7

# Quality-gate safety auto-clear timeout (seconds) — safety valve only
QUALITY_GATE_TIMEOUT_SECS     = 60

# ─── Strategy ────────────────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL  = 2
MIN_INDICATOR_VOTES     = 3
OB_EXPIRY_BARS          = 50
ATR_ZONE_FACTOR         = 3.0
NEWS_BLOCK_MINUTES      = 30
DIVERGENCE_STRENGTH_MIN = 0.3

# ─── Trade Execution ─────────────────────────────────────────────────────────
# Minimum 5 minutes so the signal structure can play out (at least 5 LTF bars).
TRADE_DURATION      = 5    # minutes — DO NOT set below 5
TRADE_DURATION_UNIT = "m"

BOOM_CRASH_TICK_DURATION = 10
BOOM_CRASH_DURATION_UNIT = "t"

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL",
                                     f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

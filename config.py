"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v11 → v12 changes:

  REMOVED — Loss-streak gate constants:
    LOSS_STREAK_PAUSE_THRESHOLD  (was -4: 1-cycle global pause)
    LOSS_STREAK_ABORT_THRESHOLD  (was -6: 3-cycle global pause + confidence≥7)
    These constants and the global-pause mechanism they drove are gone.
    Per-symbol suspension (cycle-based, isolated) replaces them.

  NEW — Per-symbol suspension:
    SYMBOL_SUSPENSION_CYCLES = 2
      When a symbol loses, it is suspended for this many full trading cycles.
      A trading cycle = TRADE_DURATION * 60 + 10 seconds.
      All other symbols continue trading normally.
      A win immediately clears the suspension counter for that symbol.

  KEPT UNCHANGED:
    LOSS_STREAK_QUALITY_GATE      – still present (tier-2 strength gate referenced
                                    by risk_manager v9 min_required_strength;
                                    no longer used but preserved for smooth rollout).
    WIN_STREAK_SCALE_THRESHOLDS   – unchanged
    WIN_STREAK_STAKE_MULTIPLIERS  – unchanged
    WIN_STREAK_CONCURRENT_BONUS   – unchanged
    MIN_CONFIDENCE_NORMAL         – unchanged (base confidence gate)
    MIN_CONFIDENCE_STRICT         – kept for reference / future use
    MIN_CONFIDENCE_RECOVERY       – kept for reference / future use
    All other v11 values preserved unchanged.

v12 → v13 changes:

  NEW — Automatic Render redeploy:
    REDEPLOY_EVERY_N_CYCLES = 5
      bot_engine increments _cycle_count at the end of every completed
      settle-wait (a cycle where ≥ 1 trade was placed).  When _cycle_count
      reaches this value the engine drains open contracts and fires the
      Render Deploy Hook stored in RENDER_DEPLOY_HOOK_URL (env var).
      Set to 0 to disable the auto-redeploy feature.

  All v12 values preserved unchanged.
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
DAILY_LOSS_LIMIT_PCT  = 7.90
RISK_PER_TRADE_PCT    = 0.2
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

# ─── Symbol Cooldown After Loss (time-based, per symbol) ─────────────────────
SYMBOL_LOSS_COOLDOWN_SECONDS     = 120
SYNTHETIC_LOSS_COOLDOWN_SECONDS  = 60

# ─── Per-Symbol Cycle Suspension After Loss ───────────────────────────────────
# When a symbol loses, it is excluded from scanning for this many full
# trading cycles.  A cycle = TRADE_DURATION * 60 + 10 seconds.
# All other symbols trade normally.  A win clears the counter immediately.
SYMBOL_SUSPENSION_CYCLES = 2

# ─── Automatic Render Redeploy ───────────────────────────────────────────────
# After this many completed trading cycles (cycles where ≥ 1 trade was placed
# and the settle-wait completed), bot_engine will:
#   1. stop opening new trades
#   2. drain all open contracts to zero
#   3. POST to RENDER_DEPLOY_HOOK_URL (env var) to trigger a fresh deploy
#   4. sleep 300 s then exit — Render replaces the process
# Set to 0 to disable.
REDEPLOY_EVERY_N_CYCLES = 2

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
MIN_CONFIDENCE_NORMAL   = 5    # normal trading conditions (active base gate)
MIN_CONFIDENCE_STRICT   = 6    # kept for reference / future use
MIN_CONFIDENCE_RECOVERY = 7    # kept for reference / future use

# ─── Loss-Streak Gate Thresholds ──────────────────────────────────────────────
# LOSS_STREAK_PAUSE_THRESHOLD and LOSS_STREAK_ABORT_THRESHOLD removed (v12).
# LOSS_STREAK_QUALITY_GATE kept below for reference only — no longer drives
# any automatic global pause or tier logic.
LOSS_STREAK_QUALITY_GATE      = -2   # (reference only — not used in v12 runtime)

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

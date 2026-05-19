"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v12 → v13 changes (BUG 3 + BUG 4 additions):

  UPDATED:
    MAX_CONCURRENT_TRADES   : 20 → 10  (more controlled parallel execution)

  NEW — Execution aggressiveness constants:
    MIN_MODULE_STRENGTH       : 3  (unconditional emission threshold)
    MIN_CONFIDENCE_FOR_PARTIAL: 5  (confidence gate for 2/3 strength signals)
    REDEPLOY_EVERY_N_CYCLES   : 6  (trigger redeploy after N complete cycles)
    SCAN_CYCLE_SLEEP          : 1  (seconds between idle scan cycles; moved from bot_engine)

  NEW — Render redeploy integration:
    RENDER_DEPLOY_HOOK_URL    : read from environment (empty string if unset)

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
DAILY_LOSS_LIMIT_PCT  = 0.90
RISK_PER_TRADE_PCT    = 0.01
MIN_STAKE             = 0.35
MAX_STAKE             = 500.0
MAX_CONCURRENT_TRADES = 10   # updated: 20 → 10

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

# ─── Symbol Cycle-Based Suspension (FIX 3 / FIX 5) ───────────────────────────
# Number of full trading cycles a symbol is suspended after a WIN or LOSS.
# Decrement_suspensions() is called once per cycle by bot_engine.
SYMBOL_WIN_SUSPENSION_CYCLES  = 2   # suspend winner for 2 cycles
SYMBOL_LOSS_SUSPENSION_CYCLES = 3   # suspend loser  for 3 cycles

# ─── Signal Score Weights (FIX 4 / FIX 5) ────────────────────────────────────
# Four-component weighted score.  Weights MUST sum to 1.0.
# Score range: 0.0 – 4.0  (max when all normalised components = 1.0)
#
# Priority hierarchy:
#   1. Module strength  (50%) — how many of m1/m2/m3 confirmed
#   2. Module quality   (30%) — how strongly each module fired
#   3. Indicator agree  (15%) — M3 indicators agreeing (lowest individual weight)
#   4. Zone freshness   ( 5%) — OB/FVG freshness
#
# Individual M3 indicators (RSI, StochRSI, MACD, BB, ADX, ATR, structure) feed
# only into SCORE_WEIGHT_INDICATOR_AGREEMENT — they never override module decisions.
SCORE_WEIGHT_MODULE_STRENGTH     = 0.50
SCORE_WEIGHT_MODULE_QUALITY      = 0.30
SCORE_WEIGHT_FRESHNESS           = 0.05
SCORE_WEIGHT_INDICATOR_AGREEMENT = 0.15

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
# New 4-component score threshold
MIN_SIGNAL_SCORE           = 2.0   # minimum composite score to pass to execution

# Legacy threshold — kept for fallback in bot_engine if MIN_SIGNAL_SCORE absent
MIN_SIGNAL_PROBABILITY     = 1.8

MIN_STRENGTH_REPEAT_SYMBOL = 3     # full 3/3 required for Round-2 repeat-symbol trades

# ─── Module Strength Thresholds ───────────────────────────────────────────────
MIN_MODULE_STRENGTH_NORMAL = 2    # minimum confirming modules under normal conditions
MIN_MODULE_STRENGTH_STRICT = 3    # minimum confirming modules under quality gate

# NEW (BUG 3): canonical threshold constants used by signal_engine
MIN_MODULE_STRENGTH      = 3    # unconditional signal emission requires 3/3 modules
MIN_CONFIDENCE_FOR_PARTIAL = 5  # 2/3 signals require at least 5/7 indicators to agree

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

# ─── Scan cycle timing ────────────────────────────────────────────────────────
# How long (seconds) to sleep between idle scan cycles (no trades placed).
# Moved from bot_engine local constant so it is centrally tuneable.
SCAN_CYCLE_SLEEP = 1

# ─── Auto-redeploy cycle budget ───────────────────────────────────────────────
# After this many completed settle-wait cycles, bot_engine will drain open
# contracts and POST to RENDER_DEPLOY_HOOK_URL to trigger a fresh deployment.
REDEPLOY_EVERY_N_CYCLES = 6

# ─── Render deploy hook ───────────────────────────────────────────────────────
# Set RENDER_DEPLOY_HOOK_URL as a Render environment variable.
# Leave empty to disable cycle-based auto-redeploy (bot continues running).
RENDER_DEPLOY_HOOK_URL = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL",
                                     f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

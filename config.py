"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v13 → v14 changes (audit pass):

  CORRECTED:
    RISK_PER_TRADE_PCT      : 0.01 → 0.02  (2% of live balance per trade)
    MAX_STAKE               : 500.0 → 50.0 (capped per spec)
    MIN_MODULE_STRENGTH     : 3 → 2         (spec: minimum 2 modules for normal signal)
    LTF_BARS                : 200 → 50      (per spec)
    DERIV_APP_ID default    : "1089" → ""   (no hardcoded app ID; must be set via env)

  ADDED:
    from typing import List  (type annotations throughout)
    BOOM_CRASH_SYMBOLS      : List[str]  — all 8 Boom/Crash instruments
    VOLATILITY_SYMBOLS      : List[str]  — all 10 Volatility Index instruments
    RANGE_BREAK_SYMBOLS     : List[str]  — RDBULL, RDBEAR
    ALL_SYMBOLS             : List[str]  — canonical scan order (Vol → B/C → RB)
    DEAD_ZONE_START_UTC     : int = 0    — Boom/Crash session exclusion start (UTC)
    DEAD_ZONE_END_UTC       : int = 5    — Boom/Crash session exclusion end (UTC)
    BOOM500_PRIME_START     : int = 7    — highest Boom/Crash liquidity window start
    BOOM500_PRIME_END       : int = 12   — highest Boom/Crash liquidity window end
    SYMBOL_SESSION_LOSS_BAN_THRESHOLD : int = 3  — losses → session ban
    WIN_STREAK_THRESHOLDS   : List[int]  — canonical alias for WIN_STREAK_SCALE_THRESHOLDS
    WIN_STREAK_MULTIPLIERS  : List[float] — canonical alias for WIN_STREAK_STAKE_MULTIPLIERS
    WIN_STREAK_EXTRA_SLOTS  : List[int]  — canonical alias for WIN_STREAK_CONCURRENT_BONUS
    WEBSOCKET_RECONNECT_INTERVAL : int = 10
    WEBSOCKET_MAX_RECONNECTS     : int = 5

  PRESERVED:
    All v13 values not listed above are unchanged.
"""

import os
from typing import List
from dotenv import load_dotenv

load_dotenv()

# ─── Deriv API ────────────────────────────────────────────────────────────────
DERIV_APP_ID    : str = os.environ.get("DERIV_APP_ID", "")          # corrected: no hardcoded fallback
DERIV_API_TOKEN : str = os.environ.get("DERIV_API_TOKEN", "")
DERIV_WS_URL    : str = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
DERIV_CURRENCY  : str = "USD"

# ─── WebSocket reconnect ──────────────────────────────────────────────────────
WEBSOCKET_RECONNECT_INTERVAL : int = 10   # seconds between reconnect attempts
WEBSOCKET_MAX_RECONNECTS     : int = 5

# ─── Symbol Groups ────────────────────────────────────────────────────────────
VOLATILITY_SYMBOLS  : List[str] = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
]

BOOM_CRASH_SYMBOLS  : List[str] = [
    "BOOM500", "CRASH500", "BOOM1000", "CRASH1000",
    "BOOM300", "CRASH300", "BOOM150", "CRASH150",
]

RANGE_BREAK_SYMBOLS : List[str] = ["RDBULL", "RDBEAR"]

# Canonical scan order: Volatility (highest freq) → Boom/Crash → Range Break
ALL_SYMBOLS : List[str] = VOLATILITY_SYMBOLS + BOOM_CRASH_SYMBOLS + RANGE_BREAK_SYMBOLS

# ─── Session / Dead Zone (UTC hours) ─────────────────────────────────────────
DEAD_ZONE_START_UTC  : int = 0    # Boom/Crash excluded 00:00–05:00 UTC
DEAD_ZONE_END_UTC    : int = 5
BOOM500_PRIME_START  : int = 7    # highest Boom/Crash liquidity window
BOOM500_PRIME_END    : int = 12

# ─── Timeframes ──────────────────────────────────────────────────────────────
HTF_GRANULARITY       : int = 3600
FOREX_LTF_GRANULARITY : int = 900
OTHER_LTF_GRANULARITY : int = 60
LTF_GRANULARITY       : int = 60
HTF_BARS              : int = 100
LTF_BARS              : int = 50    # corrected: 200 → 50 per spec

# ─── Risk Management ─────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  : float = 0.90
RISK_PER_TRADE_PCT    : float = 0.02   # corrected: 0.01 → 0.02 (2% of live balance)
MIN_STAKE             : float = 0.35
MAX_STAKE             : float = 50.0   # corrected: 500.0 → 50.0 per spec
MAX_CONCURRENT_TRADES : int   = 10

# ─── Win-Streak Stake Scaling ─────────────────────────────────────────────────
# Each element corresponds: streak ≥ threshold[i] → multiplier[i] × base stake

# Canonical names (used by all new code)
WIN_STREAK_THRESHOLDS   : List[int]   = [3, 4, 6, 8]
WIN_STREAK_MULTIPLIERS  : List[float] = [1.5, 2.0, 3.0, 4.0]
WIN_STREAK_EXTRA_SLOTS  : List[int]   = [0, 2, 4, 6]

# Legacy aliases (preserved for backward compat with modules referencing old names)
WIN_STREAK_SCALE_THRESHOLDS  = WIN_STREAK_THRESHOLDS
WIN_STREAK_STAKE_MULTIPLIERS = WIN_STREAK_MULTIPLIERS
WIN_STREAK_CONCURRENT_BONUS  = WIN_STREAK_EXTRA_SLOTS
WIN_STREAK_STAKE_FACTOR      : float = 0.30
MAX_WIN_STREAK_MULT          : float = 4.0

# ─── Symbol Cooldown After Loss ───────────────────────────────────────────────
SYMBOL_LOSS_COOLDOWN_SECONDS    : int = 120
SYNTHETIC_LOSS_COOLDOWN_SECONDS : int = 60

# ─── Symbol Cycle-Based Suspension ────────────────────────────────────────────
# Number of full trading cycles a symbol is suspended after a WIN or LOSS.
# decrement_suspensions() is called once per cycle by bot_engine.
SYMBOL_WIN_SUSPENSION_CYCLES          : int = 2   # suspend winner for 2 cycles
SYMBOL_LOSS_SUSPENSION_CYCLES         : int = 3   # suspend loser  for 3 cycles
SYMBOL_SESSION_LOSS_BAN_THRESHOLD     : int = 3   # losses in session → session ban

# ─── Signal Score Weights ─────────────────────────────────────────────────────
# Four-component weighted score.  Weights MUST sum to 1.0.
# Score range: 0.0 – 4.0  (max when all normalised components = 1.0)
#
# Priority hierarchy:
#   1. Module strength  (50%) — how many of m1/m2/m3 confirmed
#   2. Module quality   (30%) — how strongly each module fired
#   3. Indicator agree  (15%) — M3 indicators agreeing
#   4. Zone freshness   ( 5%) — OB/FVG freshness
#
# Individual M3 indicators (RSI, StochRSI, MACD, BB, ADX, ATR, structure) feed
# only into SCORE_WEIGHT_INDICATOR_AGREEMENT — they never override module decisions.
SCORE_WEIGHT_MODULE_STRENGTH     : float = 0.50
SCORE_WEIGHT_MODULE_QUALITY      : float = 0.30
SCORE_WEIGHT_FRESHNESS           : float = 0.05
SCORE_WEIGHT_INDICATOR_AGREEMENT : float = 0.15

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
MIN_SIGNAL_SCORE       : float = 2.0   # minimum composite score to pass to execution
MIN_SIGNAL_PROBABILITY : float = 1.8   # legacy fallback threshold
MIN_STRENGTH_REPEAT_SYMBOL : int = 3   # full 3/3 required for Round-2 repeat-symbol trades

# ─── Module Strength Thresholds ───────────────────────────────────────────────
MIN_MODULE_STRENGTH_NORMAL : int = 2   # minimum confirming modules, normal conditions
MIN_MODULE_STRENGTH_STRICT : int = 3   # minimum confirming modules, quality gate

# Canonical constants used by signal_engine
MIN_MODULE_STRENGTH        : int = 2   # corrected: 3 → 2  (spec: MIN_MODULE_STRENGTH = 2)
MIN_CONFIDENCE_FOR_PARTIAL : int = 5   # 2/3 signals require ≥ 5/7 indicators

# ─── Confidence Thresholds (M3 indicator agreement out of 7) ─────────────────
MIN_CONFIDENCE_NORMAL   : int = 5   # normal trading conditions
MIN_CONFIDENCE_STRICT   : int = 6   # streak ≤ LOSS_STREAK_QUALITY_GATE (tier-2/4)
MIN_CONFIDENCE_RECOVERY : int = 7   # streak ≤ LOSS_STREAK_ABORT_THRESHOLD (tier-6)

# ─── Loss-Streak Gate Thresholds ──────────────────────────────────────────────
LOSS_STREAK_QUALITY_GATE    : int = -2   # tier-2: strength=3 required
LOSS_STREAK_PAUSE_THRESHOLD : int = -4   # tier-4: 1-cycle pause + strength=3
LOSS_STREAK_ABORT_THRESHOLD : int = -6   # tier-6: 3-cycle pause + strength=3 + confidence≥7

# Quality-gate safety auto-clear timeout (seconds) — safety valve only
QUALITY_GATE_TIMEOUT_SECS : int = 60

# ─── Strategy ────────────────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL  : int   = 2
MIN_INDICATOR_VOTES     : int   = 3
OB_EXPIRY_BARS          : int   = 50
ATR_ZONE_FACTOR         : float = 3.0
NEWS_BLOCK_MINUTES      : int   = 30
DIVERGENCE_STRENGTH_MIN : float = 0.3

# ─── Trade Execution ─────────────────────────────────────────────────────────
# Minimum 5 minutes so the signal structure can play out (at least 5 LTF bars).
TRADE_DURATION      : int = 5    # minutes — DO NOT set below 5
TRADE_DURATION_UNIT : str = "m"

BOOM_CRASH_TICK_DURATION : int = 10
BOOM_CRASH_DURATION_UNIT : str = "t"

# ─── Scan Cycle Timing ────────────────────────────────────────────────────────
SCAN_CYCLE_SLEEP : int = 1   # seconds between idle scan cycles

# ─── Auto-Redeploy Cycle Budget ───────────────────────────────────────────────
# After this many completed settle-wait cycles, bot_engine will drain open
# contracts and POST to RENDER_DEPLOY_HOOK_URL to trigger a fresh deployment.
REDEPLOY_EVERY_N_CYCLES : int = 6

# ─── Render Deploy Hook ───────────────────────────────────────────────────────
# Set RENDER_DEPLOY_HOOK_URL as a Render environment variable.
# Leave empty to disable cycle-based auto-redeploy (bot continues running).
RENDER_DEPLOY_HOOK_URL : str = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")

# ─── Render Keep-Alive ────────────────────────────────────────────────────────
PORT                : int = int(os.environ.get("PORT", 8080))
SELF_URL            : str = os.environ.get("RENDER_EXTERNAL_URL",
                                            f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL : int = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL : str = os.environ.get("LOG_LEVEL", "INFO")

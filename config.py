"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v9 → v10 changes (Bug 3 tuning):

  • MIN_SIGNAL_PROBABILITY: 2.0 → 1.8.
    With the improved SMC momentum fallbacks in smc_analyzer v6 producing
    fewer NEUTRAL biases, the signal pipeline now generates more candidates.
    Lowering the threshold from 2.0 → 1.8 captures 2/3-module signals with
    a good quality bonus, increasing trade frequency on synthetics without
    compromising the structural validation already in place.

  • SYMBOL_LOSS_COOLDOWN_SECONDS: 180 → 120.
    Reducing the forex/non-synthetic cooldown after a loss from 3 minutes
    to 2 minutes. The synthetic instruments already use 90 s; 120 s brings
    forex closer to synthetic cadence to prevent over-blocking.

  • SYNTHETIC_LOSS_COOLDOWN_SECONDS: 90 → 60.
    On 1-min bars with 5-min contracts, 90 s of cooldown blocked the next
    entry opportunity. 60 s = 1 LTF bar, which is the minimum sensible
    gap before re-evaluating the symbol.

  • SCAN_CYCLE_SLEEP (bot_engine): informational note only — moved to
    bot_engine.py where it is defined (1 s, down from 2 s).

  All other values unchanged from v9.
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
HTF_GRANULARITY       = 3600   # 1 hour
FOREX_LTF_GRANULARITY = 900    # 15 min for forex pairs
OTHER_LTF_GRANULARITY = 60     # 1 min for everything else
LTF_GRANULARITY       = 60     # backward compat only
HTF_BARS              = 100
LTF_BARS              = 200

# ─── Risk Management ─────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  = 0.90   # pause only when down 90% of day-start balance
RISK_PER_TRADE_PCT    = 0.01   # base risk: 1% of current balance per trade
MIN_STAKE             = 0.35   # Deriv absolute minimum (USD)
MAX_STAKE             = 500.0  # hard cap per trade
MAX_CONCURRENT_TRADES = 20     # slots available for simultaneous trades

# ─── Win-Streak Stake Scaling ─────────────────────────────────────────────────
WIN_STREAK_STAKE_FACTOR = 0.30   # +30% of base per consecutive win
MAX_WIN_STREAK_MULT     = 4.0    # hard cap at 4× base stake

# ─── Symbol Cooldown After Loss ───────────────────────────────────────────────
SYMBOL_LOSS_COOLDOWN_SECONDS     = 120   # 2 min for forex/non-synthetic (was 180)
SYNTHETIC_LOSS_COOLDOWN_SECONDS  = 60    # 1 min for synthetics (was 90)

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
# Composite probability score = module strength (0–3) + ATR-quality bonus (0–0.5).
# Lowered to 1.8 to capture good 2/3-module signals after SMC fallback improvements.
MIN_SIGNAL_PROBABILITY     = 1.8   # lowered from 2.0 — more trades with SMC fallbacks
MIN_STRENGTH_REPEAT_SYMBOL = 3     # full 3/3 required for a second trade on same symbol

# ─── Strategy ────────────────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL  = 2
MIN_INDICATOR_VOTES     = 3    # matches signal_engine default
OB_EXPIRY_BARS          = 50   # zones must survive long enough to be hit
ATR_ZONE_FACTOR         = 3.0  # BUG 3 FIX: widened from 2.0 → 3.0 for synthetics
NEWS_BLOCK_MINUTES      = 30
DIVERGENCE_STRENGTH_MIN = 0.3

# ─── Trade Execution ─────────────────────────────────────────────────────────
# 5 minutes so the signal structure can play out (at least 5 LTF bars).
TRADE_DURATION      = 5    # minutes
TRADE_DURATION_UNIT = "m"

# BOOM/CRASH instruments use tick-based contracts.
# deriv_client.py auto-detects BOOM/CRASH and overrides to these values.
BOOM_CRASH_TICK_DURATION = 10    # number of ticks for tick contracts
BOOM_CRASH_DURATION_UNIT = "t"  # "t" = ticks (Deriv API duration_unit)

# ─── Loss-Streak Quality Gate ─────────────────────────────────────────────────
LOSS_STREAK_QUALITY_GATE  = -3   # activate quality gate at streak <= -3
QUALITY_GATE_TIMEOUT_SECS = 60   # force-clear gate after N seconds as a safety valve

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL",
                                     f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

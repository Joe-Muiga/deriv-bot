"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v8 → v9 changes (Priority 4):

  • TRADE_DURATION: 3 → 5 minutes.
    The signal is built on 1-min and 60-min bars.  A 3-minute contract
    expires before the LTF structure has time to play out.  5 minutes gives
    the trade room to develop without becoming a slow "hold for hours" bet.

  • BOOM_CRASH_TICK_DURATION: NEW (10 ticks).
    BOOM/CRASH contracts are auto-detected in deriv_client.py and switched
    to tick-based resolution.  A tick contract wins or loses the moment
    price ticks in the right direction N times — perfectly suited for the
    spike-pattern nature of BOOM/CRASH instruments.

  • BOOM_CRASH_DURATION_UNIT: NEW ("t").
    Duration unit override for BOOM/CRASH — passed to the Deriv API.

  • MIN_SIGNAL_PROBABILITY: 1.8 → 2.0.
    Raised back after the self-validation step in signal_engine was added.
    The validation already filters noise; the probability gate can be
    stricter again to ensure only high-quality setups execute.

  • SYNTHETIC_LOSS_COOLDOWN_SECONDS: 120 → 90.
    With improved signal quality, 90 seconds is sufficient for synthetics.

  • WIN_STREAK_STAKE_FACTOR / MAX_WIN_STREAK_MULT: unchanged (0.30 / 4.0).
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
# Win streak  → stake = base × (1.0 + streak × WIN_STREAK_STAKE_FACTOR),
#               capped at base × MAX_WIN_STREAK_MULT.
# Loss streak → stake forced to MIN_STAKE (quality gate kicks in at -3).
WIN_STREAK_STAKE_FACTOR = 0.30   # +30% of base per consecutive win
MAX_WIN_STREAK_MULT     = 4.0    # hard cap at 4× base stake

# ─── Symbol Cooldown After Loss ───────────────────────────────────────────────
SYMBOL_LOSS_COOLDOWN_SECONDS     = 180   # 3 minutes for forex/non-synthetic
SYNTHETIC_LOSS_COOLDOWN_SECONDS  = 90    # 1.5 minutes — tightened from 120

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
# Composite probability score = module strength (0–3) + ATR-quality bonus (0–0.5).
# Raised back to 2.0 now that self-validation filters signal noise.
MIN_SIGNAL_PROBABILITY     = 2.0   # raised from 1.8 — validation handles noise
MIN_STRENGTH_REPEAT_SYMBOL = 3     # full 3/3 required for a second trade on same symbol

# ─── Strategy ────────────────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL  = 2
MIN_INDICATOR_VOTES     = 3    # matches signal_engine default
OB_EXPIRY_BARS          = 50   # zones must survive long enough to be hit
ATR_ZONE_FACTOR         = 2.0  # widened tolerance for synthetic instruments
NEWS_BLOCK_MINUTES      = 30
DIVERGENCE_STRENGTH_MIN = 0.3

# ─── Trade Execution ─────────────────────────────────────────────────────────
# Priority 4: raised from 3 → 5 minutes so the signal structure can play out.
# The 5-min window covers at least 5 LTF (1-min) bars after entry, giving
# the momentum shift identified by the three modules time to materialise.
TRADE_DURATION      = 5    # minutes — raised from 3
TRADE_DURATION_UNIT = "m"

# Priority 4: BOOM/CRASH instruments use tick-based contracts.
# deriv_client.py auto-detects BOOM/CRASH and overrides to these values.
# 10 ticks is typically resolved in < 30 seconds on volatile synthetics.
BOOM_CRASH_TICK_DURATION = 10    # number of ticks for tick contracts
BOOM_CRASH_DURATION_UNIT = "t"  # "t" = ticks (Deriv API duration_unit)

# ─── Loss-Streak Quality Gate ─────────────────────────────────────────────────
# When the loss streak reaches this threshold, the RiskManager activates a
# quality gate that blocks trades unless signal_strength == 3 (all 3 modules
# confirm).  Streak resets on next win and gate deactivates automatically.
# bot_engine should call risk.can_trade(signal_strength=sig.strength) to use this.
LOSS_STREAK_QUALITY_GATE  = -3   # activate quality gate at streak <= -3
QUALITY_GATE_TIMEOUT_SECS = 60   # force-clear gate after N seconds as a safety valve

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL",
                                     f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

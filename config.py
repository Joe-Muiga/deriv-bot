"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.
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
HTF_GRANULARITY       = 3600   # 1 hour (seconds) – used for all symbols
FOREX_LTF_GRANULARITY = 900    # 15 min for forex pairs
OTHER_LTF_GRANULARITY = 60     # 1 min for crypto, synthetics, metals, indices
# LTF_GRANULARITY kept for backward compat with test_local.py only
LTF_GRANULARITY       = 60
HTF_BARS              = 100    # historical HTF bars to fetch
LTF_BARS              = 200    # historical LTF bars to fetch

# ─── Risk Management ─────────────────────────────────────────────────────────
# Trading pauses ONLY when balance drops 90% below day-start balance.
# No other balance-based throttle exists.
DAILY_LOSS_LIMIT_PCT  = 0.90   # pause if down 90% of day-start balance
RISK_PER_TRADE_PCT    = 0.01   # risk 1% of current balance per trade
MIN_STAKE             = 0.35   # Deriv's absolute minimum stake (USD)
MAX_STAKE             = 500.0  # hard cap per trade for safety
MAX_CONCURRENT_TRADES = 10     # raised to allow high throughput

# ─── Strategy ────────────────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL  = 2    # need ≥ 2 / 3 SIFM modules to fire
MIN_INDICATOR_VOTES     = 4    # Module-3: need ≥ 4 / 7 indicators aligned
OB_EXPIRY_BARS          = 20   # order-block expires after N HTF bars
ATR_ZONE_FACTOR         = 0.5  # price must be within 0.5×ATR of SMC zone
NEWS_BLOCK_MINUTES      = 30   # silence trading 30 min before high-impact news
DIVERGENCE_STRENGTH_MIN = 0.3  # minimum normalised divergence strength

# ─── Trade Execution ──────────────────────────────────────────────────────────
TRADE_DURATION      = 5    # contract duration value
TRADE_DURATION_UNIT = "m"  # "t"=ticks, "s"=sec, "m"=min, "h"=hr, "d"=day

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Render auto-redeploy ────────────────────────────────────────────────────
RENDER_DEPLOY_HOOK_URL = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

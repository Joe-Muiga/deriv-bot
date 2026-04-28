"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Deriv API ────────────────────────────────────────────────────────────────
DERIV_APP_ID    = os.environ.get("DERIV_APP_ID", "1089")       # Create free app at https://api.deriv.com
DERIV_API_TOKEN = os.environ.get("DERIV_API_TOKEN", "")        # Your Deriv API token (read+trade scope)
DERIV_WS_URL    = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
DERIV_CURRENCY  = "USD"

# ─── Timeframes ──────────────────────────────────────────────────────────────
HTF_GRANULARITY  = 3600   # 1 hour (seconds)
LTF_GRANULARITY  = 300    # 5 minutes (seconds)
HTF_BARS         = 100    # historical HTF bars to fetch
LTF_BARS         = 200    # historical LTF bars to fetch

# ─── Risk Management ─────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  = 0.09   # pause if down 9 % of day-start balance
RISK_PER_TRADE_PCT    = 0.01   # risk 1 % of current balance per trade
MIN_STAKE             = 0.35   # Deriv's absolute minimum stake (USD)
MAX_STAKE             = 500.0  # hard cap per trade for safety
MAX_CONCURRENT_TRADES = 3      # open positions cap

# ─── Strategy ────────────────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL = 2     # need ≥ 2 / 3 SIFM modules to fire
MIN_INDICATOR_VOTES    = 5     # Module-3: need ≥ 5 / 7 indicators aligned
OB_EXPIRY_BARS         = 20    # order-block expires after N HTF bars
ATR_ZONE_FACTOR        = 0.5   # price must be within 0.5×ATR of SMC zone
NEWS_BLOCK_MINUTES     = 30    # silence trading 30 min before high-impact news
DIVERGENCE_STRENGTH_MIN = 0.3  # minimum normalised divergence strength

# ─── Trade Execution ──────────────────────────────────────────────────────────
TRADE_DURATION      = 5    # contract duration value
TRADE_DURATION_UNIT = "m"  # "t"=ticks, "s"=sec, "m"=min, "h"=hr, "d"=day

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40   # ping self every 40 seconds (Render sleeps at 15 min idle)

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

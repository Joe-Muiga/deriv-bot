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
HTF_GRANULARITY  = 3600   # 1 hour
LTF_GRANULARITY  = 300    # 5 minutes
HTF_BARS         = 100
LTF_BARS         = 200

# ─── Risk Management ─────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  = 0.09
RISK_PER_TRADE_PCT    = 0.01
MIN_STAKE             = 0.35
MAX_STAKE             = 500.0
MAX_CONCURRENT_TRADES = 3

# ─── Strategy (RELAXED for realistic signal generation) ──────────────────────
MIN_MODULES_FOR_SIGNAL = 1     # CHANGED: 1/3 modules enough to trade (was 2)
MIN_INDICATOR_VOTES    = 4     # CHANGED: 4/7 indicators (was 5/7)
OB_EXPIRY_BARS         = 50    # CHANGED: OBs live longer (was 20)
ATR_ZONE_FACTOR        = 1.0   # CHANGED: wider zone tolerance (was 0.5)
NEWS_BLOCK_MINUTES     = 30
DIVERGENCE_STRENGTH_MIN = 0.1  # CHANGED: easier divergence (was 0.3)

# ─── Trade Execution ──────────────────────────────────────────────────────────
TRADE_DURATION      = 5
TRADE_DURATION_UNIT = "m"

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

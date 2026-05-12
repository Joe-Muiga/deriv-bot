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
HTF_GRANULARITY  = 3600   # 1 hour  – structure/bias detection
LTF_GRANULARITY  = 60     # 1 MINUTE – was 5M; 5x more candles = 5x more signals
HTF_BARS         = 100
LTF_BARS         = 300    # 300 x 1M = 5 hours of LTF history

# ─── Risk Management ─────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  = 0.09
RISK_PER_TRADE_PCT    = 0.01
MIN_STAKE             = 0.50   # Deriv minimum
MAX_STAKE             = 500.0
MAX_CONCURRENT_TRADES = 1      # Keep at 1 while balance is low

# ─── Strategy ────────────────────────────────────────────────────────────────
# ACCURACY: require 2/3 modules (strict)
# FREQUENCY: comes from 1M LTF + dual-EMA module that fires continuously in trends
MIN_MODULES_FOR_SIGNAL  = 2     # Accuracy gate — do NOT lower this
MIN_INDICATOR_VOTES     = 4     # 4/7 indicators must agree
OB_EXPIRY_BARS          = 35    # Balanced (was 20 orig, 50 relaxed)
ATR_ZONE_FACTOR         = 0.75  # Balanced zone width
NEWS_BLOCK_MINUTES      = 30
DIVERGENCE_STRENGTH_MIN = 0.15

# ─── Trade Execution ──────────────────────────────────────────────────────────
TRADE_DURATION      = 5
TRADE_DURATION_UNIT = "m"

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

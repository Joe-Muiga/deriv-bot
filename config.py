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
# HTF is uniform across all instruments (1-hour structure / bias detection)
HTF_GRANULARITY          = 3600   # 1 hour
HTF_BARS                 = 100

# LTF varies by instrument class to maximise frequency without noise.
#   Forex pairs  (frx* prefix) – 15-min bars are the sweet spot:
#     enough data for reliable indicator readings while still generating
#     several actionable signals per session.
#   Synthetics / Crypto / Indices – 1-min bars for maximum frequency.
LTF_GRANULARITY_FOREX      = 900   # 15 min for forex pairs
LTF_GRANULARITY_SYNTHETIC  = 60    # 1 min  for synthetics, crypto, indices

LTF_BARS_FOREX             = 200   # 200 × 15 min ≈ 50 h of LTF history
LTF_BARS_SYNTHETIC         = 300   # 300 ×  1 min =  5 h of LTF history

# ─── Risk Management ─────────────────────────────────────────────────────────
# Trading pauses for the remainder of the UTC day once the account has lost
# 90 % of the day-start balance (i.e. only ~10 % of capital remains).
DAILY_LOSS_LIMIT_PCT  = 0.90   # 90 % drawdown → pause until UTC midnight
RISK_PER_TRADE_PCT    = 0.01   # 1 % of current balance per trade
MIN_STAKE             = 0.50   # Deriv minimum
MAX_STAKE             = 500.0
MAX_CONCURRENT_TRADES = 5      # Up to 5 open positions at once

# ─── Strategy ────────────────────────────────────────────────────────────────
# ACCURACY : require 2-of-3 signal modules (strict gate)
# FREQUENCY: comes from parallel scanning all symbols every SCAN_INTERVAL
MIN_MODULES_FOR_SIGNAL  = 2     # Accuracy gate — do NOT lower this
MIN_INDICATOR_VOTES     = 4     # 4/7 indicators must agree
OB_EXPIRY_BARS          = 35
ATR_ZONE_FACTOR         = 0.75
NEWS_BLOCK_MINUTES      = 30
DIVERGENCE_STRENGTH_MIN = 0.15

# ─── Trade Execution ──────────────────────────────────────────────────────────
TRADE_DURATION      = 5
TRADE_DURATION_UNIT = "m"

# ─── Parallel Scanning ────────────────────────────────────────────────────────
SCAN_INTERVAL        = 3    # seconds between full parallel scan cycles
MAX_SYMBOLS_PER_QUEUE = 50  # how many symbols to keep in the active queue
INIT_BATCH_SIZE      = 5    # symbols initialised concurrently per batch
INIT_BATCH_DELAY     = 0.5  # seconds of rest between init batches

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

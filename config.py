"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

High Confidence Mode (HCM)
---------------------------
The bot consistently wins its first few trades per session because it
naturally waits for a fully-formed, high-confluence setup before anything
clears all filters.  HCM replicates that behaviour permanently:

  • Active for the first HCM_DAILY_TRADE_COUNT completed trades each day.
  • Re-activates after HCM_LOSS_TRIGGER consecutive losses.
  • Requires all 3 signal modules + HCM_MIN_VOTES indicator votes.
  • Executes only the single best-scored signal per scan cycle.
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
HTF_GRANULARITY            = 3600   # 1-hour HTF structure / bias detection
HTF_BARS                   = 100

# LTF varies by asset class for optimal signal density without noise.
#   Forex pairs  (frx* prefix) → 15-min bars (well-established patterns)
#   Synthetics / Crypto / rest → 1-min  bars (maximum frequency)
LTF_GRANULARITY_FOREX      = 900    # 15 min
LTF_GRANULARITY_SYNTHETIC  = 60     # 1  min

LTF_BARS_FOREX             = 200    # 200 × 15 min ≈ 50 h
LTF_BARS_SYNTHETIC         = 300    # 300 ×  1 min =  5 h

# ─── Risk Management ─────────────────────────────────────────────────────────
# Pause until UTC midnight once 90 % of the day-start balance has been lost.
DAILY_LOSS_LIMIT_PCT  = 0.90
RISK_PER_TRADE_PCT    = 0.01        # 1 % of current balance per trade
MIN_STAKE             = 0.50        # Deriv minimum
MAX_STAKE             = 500.0
MAX_CONCURRENT_TRADES = 4           # Keep at 1 — losses compound quickly.
                                    # Raise only after win-rate exceeds 55 %.

# ─── Standard Signal Quality ─────────────────────────────────────────────────
# Active after HCM ends and while consecutive losses < HCM_LOSS_TRIGGER.
MIN_MODULES_FOR_SIGNAL  = 3         # 2-of-3 modules must confirm
MIN_INDICATOR_VOTES     = 5         # 5-of-7 indicators must agree (raised from 4)

# ─── High Confidence Mode (HCM) ──────────────────────────────────────────────
# Replicates the "startup advantage": wait for complete, unambiguous confluence.
HCM_DAILY_TRADE_COUNT = 3           # first N completed trades of the day
HCM_LOSS_TRIGGER      = 1           # consecutive losses that re-activate HCM
HCM_MIN_MODULES       = 3           # ALL 3 modules must agree
HCM_MIN_VOTES         = 5           # 5/7 indicators (same as normal but
                                    # combined with the 3/3 module gate this
                                    # is dramatically stricter overall)
HCM_MAX_EXECUTE       = 1           # only the single top-scored signal fires

# ─── Strategy ────────────────────────────────────────────────────────────────
OB_EXPIRY_BARS          = 35
ATR_ZONE_FACTOR         = 0.75
NEWS_BLOCK_MINUTES      = 5
DIVERGENCE_STRENGTH_MIN = 0.15

# Minimum LTF bars that must elapse between two trades on the SAME symbol.
# Prevents re-entering the same failing setup on consecutive bars.
MIN_BARS_BETWEEN_SAME_SYMBOL = 3

# ─── Trade Execution ─────────────────────────────────────────────────────────
TRADE_DURATION      = 5
TRADE_DURATION_UNIT = "m"

# ─── Parallel Scanning ───────────────────────────────────────────────────────
SCAN_INTERVAL         = 3           # seconds between full parallel scan cycles
MAX_SYMBOLS_PER_QUEUE = 50
INIT_BATCH_SIZE       = 5           # concurrent symbol initialisations
INIT_BATCH_DELAY      = 0.5         # seconds between init batches

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

"""
config.py – SIFM Deriv Trading Bot configuration.
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
HTF_GRANULARITY           = 3600   # 1-hour HTF for all symbols
HTF_BARS                  = 100
LTF_GRANULARITY_FOREX     = 900    # 15-min LTF for forex pairs
LTF_GRANULARITY_SYNTHETIC = 60     # 1-min  LTF for synthetics
LTF_BARS_FOREX            = 200
LTF_BARS_SYNTHETIC        = 300

# ─── Symbol filtering ────────────────────────────────────────────────────────
# Pure Volatility Indices (R_*) are random-walk instruments.
# SMC has no edge on them. They are excluded from the scan entirely.
# Only instruments with genuine trending structure are traded.
SMC_ELIGIBLE_SYNTHETICS = [
    "BOOM500",  "BOOM1000",    # Boom indices  – strong upward trend + spikes
    "CRASH500", "CRASH1000",   # Crash indices – strong downward trend + spikes
    "stpRNG",                  # Step index    – structured movement
]
# Any symbol whose name starts with these prefixes is EXCLUDED
EXCLUDED_PREFIXES = ("R_", "1HZ")   # Volatility indices – random walk, skip

# ─── Risk ────────────────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT  = 0.90   # pause at 90% drawdown from day start
RISK_PER_TRADE_PCT    = 0.01
MIN_STAKE             = 0.50
MAX_STAKE             = 500.0
MAX_CONCURRENT_TRADES = 999    # no concurrent cap – only balance/loss-limit stops trades

# ─── Signal quality (hardcoded – never lower these) ──────────────────────────
MIN_MODULES_FOR_SIGNAL = 3     # ALL 3 modules must agree
MIN_INDICATOR_VOTES    = 5     # 5-of-7 indicators must agree

# ─── Strategy ────────────────────────────────────────────────────────────────
OB_EXPIRY_BARS                  = 35
ATR_ZONE_FACTOR                 = 0.75
NEWS_BLOCK_MINUTES              = 30
DIVERGENCE_STRENGTH_MIN         = 0.15
MIN_SECONDS_BETWEEN_SAME_SYMBOL = 60   # 1-min cooldown per symbol (was 300)

# ─── Trade execution ─────────────────────────────────────────────────────────
TRADE_DURATION      = 5
TRADE_DURATION_UNIT = "m"

# ─── Scanning ────────────────────────────────────────────────────────────────
SCAN_INTERVAL         = 3
MAX_SYMBOLS_PER_QUEUE = 50
INIT_BATCH_SIZE       = 5
INIT_BATCH_DELAY      = 0.5

# ─── Render ──────────────────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40
LOG_LEVEL           = os.environ.get("LOG_LEVEL", "INFO")

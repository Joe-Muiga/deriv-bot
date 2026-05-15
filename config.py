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
MAX_CONCURRENT_TRADES = 10

# ─── Win-Streak Stake Scaling ─────────────────────────────────────────────────
# Win streak  → stake = base × (1.0 + streak × WIN_STREAK_STAKE_FACTOR),
#               capped at base × MAX_WIN_STREAK_MULT.
# Loss streak → stake forced to MIN_STAKE regardless of balance.
WIN_STREAK_STAKE_FACTOR = 0.25   # +25% of base per consecutive win
MAX_WIN_STREAK_MULT     = 3.0    # hard cap at 3× base stake

# ─── Symbol Cooldown After Loss ───────────────────────────────────────────────
# After ANY losing trade on a symbol, that symbol is blocked for this many
# seconds before it can be considered for a new trade again.
SYMBOL_LOSS_COOLDOWN_SECONDS = 900   # 15 minutes

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
# Composite probability score = module strength (0–3) + ATR-quality bonus (0–0.5).
# Trades are only executed when score >= MIN_SIGNAL_PROBABILITY.
MIN_SIGNAL_PROBABILITY     = 2.0   # needs ≥ 2/3 modules + clean setup

# A second trade on the SAME symbol in the same cycle requires full 3/3 modules.
MIN_STRENGTH_REPEAT_SYMBOL = 3

# ─── Strategy ────────────────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL  = 2
MIN_INDICATOR_VOTES     = 4
OB_EXPIRY_BARS          = 20
ATR_ZONE_FACTOR         = 0.5
NEWS_BLOCK_MINUTES      = 30
DIVERGENCE_STRENGTH_MIN = 0.3

# ─── Trade Execution ─────────────────────────────────────────────────────────
TRADE_DURATION      = 5
TRADE_DURATION_UNIT = "m"

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

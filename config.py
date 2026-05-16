"""
config.py – Centralised configuration for the SIFM Deriv Trading Bot.
All secrets are loaded from environment variables; defaults are safe fallbacks.

v6 → v7 tuning for high-frequency synthetic trading:
  • SYMBOL_LOSS_COOLDOWN_SECONDS: 900 → 180  (15 min → 3 min; synthetics recover fast)
  • SYNTHETIC_LOSS_COOLDOWN_SECONDS: 120      (new — 2 min for pure-synthetic symbols)
  • OB_EXPIRY_BARS: 20 → 50                  (zones must survive long enough to be hit)
  • MAX_CONCURRENT_TRADES: 10 → 20           (more slots to compound gains faster)
  • MIN_INDICATOR_VOTES: 4 → 3               (match signal_engine default; was silently
                                              overriding the relaxed threshold)
  • ATR_ZONE_FACTOR: 0.5 → 2.0              (match smc_analyzer widened tolerance)
  • TRADE_DURATION: 5 → 3                   (shorter contract, faster PnL resolution)
  • WIN_STREAK_STAKE_FACTOR: 0.25 → 0.30    (compound faster on win streaks)
  • MAX_WIN_STREAK_MULT: 3.0 → 4.0          (allow larger compound multiples)
  • MIN_SIGNAL_PROBABILITY: 2.0 → 1.8       (let strong 2-module signals through)
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
MAX_CONCURRENT_TRADES = 20     # increased from 10 → compound gains faster

# ─── Win-Streak Stake Scaling ─────────────────────────────────────────────────
# Win streak  → stake = base × (1.0 + streak × WIN_STREAK_STAKE_FACTOR),
#               capped at base × MAX_WIN_STREAK_MULT.
# Loss streak → stake forced to MIN_STAKE regardless of balance.
WIN_STREAK_STAKE_FACTOR = 0.30   # +30% of base per consecutive win (was 0.25)
MAX_WIN_STREAK_MULT     = 4.0    # hard cap at 4× base stake (was 3.0)

# ─── Symbol Cooldown After Loss ───────────────────────────────────────────────
# After ANY losing trade on a symbol, that symbol is blocked for this many
# seconds before it can be considered for a new trade again.
SYMBOL_LOSS_COOLDOWN_SECONDS = 180    # 3 minutes (was 900 / 15 min)

# Synthetic instruments recover direction faster — apply a shorter cooldown.
# Used by SymbolManager.record_trade() when the symbol is in sym_module.SYNTHETIC.
SYNTHETIC_LOSS_COOLDOWN_SECONDS = 120  # 2 minutes

# ─── Signal Quality Gate ──────────────────────────────────────────────────────
# Composite probability score = module strength (0–3) + ATR-quality bonus (0–0.5).
# Trades are only executed when score >= MIN_SIGNAL_PROBABILITY.
MIN_SIGNAL_PROBABILITY     = 1.8   # lowered from 2.0; 2 modules + small bonus passes
MIN_STRENGTH_REPEAT_SYMBOL = 3     # full 3/3 still required for a second trade

# ─── Strategy ────────────────────────────────────────────────────────────────
MIN_MODULES_FOR_SIGNAL  = 2
MIN_INDICATOR_VOTES     = 3    # lowered from 4 → matches signal_engine default
OB_EXPIRY_BARS          = 50   # increased from 20; zones must survive to be hit
ATR_ZONE_FACTOR         = 2.0  # widened from 0.5; matches smc_analyzer tolerance
NEWS_BLOCK_MINUTES      = 30
DIVERGENCE_STRENGTH_MIN = 0.3

# ─── Trade Execution ─────────────────────────────────────────────────────────
TRADE_DURATION      = 3    # minutes — reduced from 5; faster PnL resolution
TRADE_DURATION_UNIT = "m"

# ─── Render keep-alive ───────────────────────────────────────────────────────
PORT                = int(os.environ.get("PORT", 8080))
SELF_URL            = os.environ.get("RENDER_EXTERNAL_URL",
                                     f"http://localhost:{PORT}")
KEEP_ALIVE_INTERVAL = 40

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

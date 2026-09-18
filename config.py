"""
config.py — SMC / ICT trading bot configuration.

COMPLETE REWRITE (Sep 2026 pivot). Every synthetic-index-specific config
section from the previous version has been DELETED, not commented out or
flag-disabled:

  - Symbol universe: VOLATILITY_STANDARD, VOLATILITY_1S, BOOM_CRASH, STEP,
    JUMP, DRIFT, BEAR/BULL (RDBEAR/RDBULL), RANGE_BREAK, DIGIT_SYMBOLS,
    DIGIT_PARITY_SYMBOLS, MEAN_REVERSION_SYMBOLS, VOL_MULTIPLIER_SYMBOLS,
    PULLBACK_TREND/FAST_MEAN_REV/SPIKE_CATCH_1000/SPIKE_CATCH_500/
    STEP_GRID_SYMBOLS and every per-strategy tuning constant that went
    with them (PULLBACK_*, SCALP_*, SPIKE_CATCH_*, STEP_GRID_*,
    POPULAR_CONFLUENCE_*, VOL_REGIME_*, BREAKOUT_MARGIN_ATR,
    MEAN_REV_REQUIRE_TURN, etc.)
  - Entry-transform hacks built for synthetic-index momentum signals:
    STOP_AS_TRIGGER_*, FLIP_ENTRY_*, DELAYED_ENTRY_*, FIXED_ENTRY_*,
    SCALED_SL_TP_*, STOP_LOSS_MIDPOINT_PCT, INVERT_ALL_SIGNALS,
    TP_SL_SWAP_ENABLED, RESTRICT_TRADING_TO_STOP_AS_TRIGGER_SYMBOLS. A
    genuine SMC/ICT signal's direction and structural stop/target ARE the
    analysis — flipping or rescaling them the way these did for synthetic
    momentum strategies would throw away the entire basis for the trade.
  - The dead TAKE/INVERT Bayesian bandit config (META_LABEL_INVERT_ENABLED,
    META_LABEL_DEFAULT_ACTION_BY_SYMBOL/_FALLBACK, BAYESIAN_*,
    INVERT_MIN_CONFIDENCE) — confirmed via grep that
    meta_labeling.predict_take_trade() (the only consumer) was already
    disconnected from bot_engine.py's execution path before this pivot
    (its own docstring says so); removing the config that fed it closes
    off any risk of it being silently reconnected with synthetic-tuned
    per-symbol biases.
  - PAIR_SUSPEND_MINUTES (pair_suspension.py, built for the multi-
    indicator "which indicator gets picked" pipeline, has been deleted —
    there's one strategy now, so per-(indicator, symbol) suspension isn't
    a meaningful concept; plain per-symbol suspension in symbol_manager.py
    covers it).
  - Dead/unused clutter confirmed by grep against the whole codebase:
    MIN_MODULES_FOR_SIGNAL, MIN_INDICATOR_VOTES, MIN_SIGNAL_PROBABILITY,
    MIN_STRENGTH_REPEAT_SYMBOL, MIN_SCORE, MIN_CONFLUENCE,
    MIN_MODULE_STRENGTH(_NORMAL), MIN_CONFIDENCE_NORMAL,
    MIN_CONFIDENCE_FOR_PARTIAL, MIN_STRATEGY_AGREEMENT, the placeholder
    "SMC parameters" block (OB_LOOKBACK/FVG_MIN_ATR/SWEEP_LOOKBACK/
    SWING_LOOKBACK/FIB_*/EMA_*/RSI_*/MOMENTUM_LOOKBACK/BREAKOUT_ATR_MULT —
    superseded by ict_engine.py's own constants), ACCU_* (accumulator
    contracts), DEAD_ZONE_*/BOOM500_PRIME_* (synthetic session windows),
    DIGIT_HYBRID_MODE, the old synthetic-symbol PRIORITY_SYMBOLS list, and
    the synthetic-keyed SESSION_DOW_WEIGHT_TABLE.

What's new: the "ICT / SMC TRADING UNIVERSE" and "ICT ENGINE TUNING"
sections below. Everything else (risk sizing, concurrency, reconciliation,
exit engine, dashboard/redeploy plumbing) is the same generic
infrastructure as before, since none of it was synthetic-specific to
begin with — it operates on whatever symbol list ALL_TRADE_SYMBOLS points
at, which is now symbols.ICT_TRADING_UNIVERSE.
"""

import os
import symbols as sym_module

# ══════════════════════════════════════════════════════════════
# GENERAL / DERIV API / SERVER
# ══════════════════════════════════════════════════════════════
LOG_LEVEL = "INFO"
DEBUG     = False
VERSION   = "2.0.0"   # SMC/ICT pivot

DERIV_API_TOKEN = os.environ.get("DERIV_API_TOKEN", "")
DERIV_APP_ID    = os.environ.get("DERIV_APP_ID", "1089")
DERIV_WS_URL: str = os.environ.get(
    "DERIV_WS_URL",
    f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
)

PORT = int(os.environ.get("PORT", 10000))
SELF_URL = os.environ.get("SELF_URL", os.environ.get("RENDER_EXTERNAL_URL", ""))
KEEP_ALIVE_INTERVAL = 600  # seconds between self-ping requests


# ══════════════════════════════════════════════════════════════
# ICT / SMC TRADING UNIVERSE
# ══════════════════════════════════════════════════════════════
# The 11-symbol ICT/SMC universe. See symbols.py for the list definitions
# (MAJOR_FOREX / GOLD / MAJOR_COMMODITIES / ICT_TRADING_UNIVERSE).
# ICT_TRADING_UNIVERSE itself is untouched by the stpRNG addition below —
# see "STEP GRID (stpRNG) — INDEPENDENT PARALLEL STRATEGY" for how stpRNG
# is added to the scanned/tradeable set without merging into this list or
# routing through evaluate_ict().
TRADE_SYMBOLS      = list(sym_module.ICT_TRADING_UNIVERSE)
ALL_TRADE_SYMBOLS  = list(sym_module.ICT_TRADING_UNIVERSE)
ALL_SYMBOLS        = list(sym_module.ICT_TRADING_UNIVERSE)
VOLATILITY_SYMBOLS = list(sym_module.ICT_TRADING_UNIVERSE)  # alias bot_engine.py/symbol_manager.py read

# Every symbol in the universe trades via Multiplier contracts
# (MULTUP/MULTDOWN) — ICT/SMC needs a real, price-based stop-loss and
# take-profit (the entry-OB extreme and the next liquidity pool), which a
# fixed-duration Rise/Fall digital option cannot express at all (it
# settles on a timer, not a price level). See deriv_client.buy_multiplier()
# and signal_engine.SignalResult's native_stop_price/native_target_price/
# native_entry_price fields, which ict_engine.analyze() always populates.
MULTIPLIER_SYMBOLS = list(sym_module.ICT_TRADING_UNIVERSE)
RISE_FALL_SYMBOLS  = []   # nothing trades Rise/Fall in this bot anymore


# ══════════════════════════════════════════════════════════════
# STEP GRID (stpRNG) — INDEPENDENT PARALLEL STRATEGY
# ══════════════════════════════════════════════════════════════
# stpRNG (Deriv's Step Index) trades as a 12th symbol, in full parallel to
# the 11 ICT symbols above, via its own standalone evaluator
# (signal_engine.evaluate_step_grid_final()) — it never touches
# evaluate_ict() / ict_engine.py / smc_analyzer.py. Deliberately NOT
# merged into ICT_TRADING_UNIVERSE/MAJOR_FOREX/GOLD/MAJOR_COMMODITIES —
# those stay exactly as they were. This is the ONLY place stpRNG is added
# to the bot's scanned/tradeable symbol set; TRADE_SYMBOLS/
# ALL_TRADE_SYMBOLS/ALL_SYMBOLS/VOLATILITY_SYMBOLS/MULTIPLIER_SYMBOLS
# below are widened additively so bot_engine.py's startup init and
# symbol_manager.py's active-symbol list pick it up automatically, and
# signal_engine.SignalEngine.evaluate() checks STEP_GRID_SYMBOLS ahead of
# the ICT_TRADING_UNIVERSE check so stpRNG never falls into the ICT branch.
STEP_GRID_SYMBOLS = ["stpRNG"]

TRADE_SYMBOLS      = list(dict.fromkeys(TRADE_SYMBOLS      + STEP_GRID_SYMBOLS))
ALL_TRADE_SYMBOLS  = list(dict.fromkeys(ALL_TRADE_SYMBOLS  + STEP_GRID_SYMBOLS))
ALL_SYMBOLS        = list(dict.fromkeys(ALL_SYMBOLS        + STEP_GRID_SYMBOLS))
VOLATILITY_SYMBOLS = list(dict.fromkeys(VOLATILITY_SYMBOLS + STEP_GRID_SYMBOLS))
# stpRNG also trades via Multiplier contracts (its flip-entry transform
# below produces real native_entry_price/native_stop_price/
# native_target_price, exactly like ict_engine.analyze() does) — added to
# MULTIPLIER_SYMBOLS so bot_engine._execute() routes it through
# buy_multiplier() the same way it does the 11 ICT symbols.
MULTIPLIER_SYMBOLS = list(dict.fromkeys(MULTIPLIER_SYMBOLS + STEP_GRID_SYMBOLS))

# The exact stpRNG AND-gate strategy (evaluate_step_grid() in
# signal_engine.py), ported verbatim from the synthetic-indices bot —
# a dedicated block, not shared with/read by any ICT config above.
STEP_GRID_MIN_BARS               = 30
STEP_GRID_EMA_FAST_PERIOD        = 10
STEP_GRID_EMA_SLOW_PERIOD        = 20
STEP_GRID_RSI_PERIOD             = 14
STEP_GRID_RSI_LONG_MIN           = 55.0
STEP_GRID_RSI_SHORT_MAX          = 45.0
STEP_GRID_MACD_FAST              = 12
STEP_GRID_MACD_SLOW              = 26
STEP_GRID_MACD_SIGNAL            = 9
STEP_GRID_RANGE_LOOKBACK         = 20
STEP_GRID_STOP_BUFFER_ATR_MULT   = 0.30
STEP_GRID_RR_RATIO               = 2.0

# The flip-entry transform applied to stpRNG's raw signal before
# execution (signal_engine._apply_flip_and_swap_levels()) — confirmed
# live/profitable yesterday in the synthetic-indices bot. stpRNG-scoped
# only; not applied to, and not read by, any ICT symbol.
FLIP_ENTRY_MIN_RR_RATIO      = 2.0
FLIP_ENTRY_SL_SAFETY_MARGIN  = 0.10

# Priority order for INIT_BATCH_SIZE-batched startup — gold and the EUR/USD,
# GBP/USD, USD/JPY majors first (deepest liquidity / most actively traded),
# then the rest.
PRIORITY_SYMBOLS = [
    "frxXAUUSD", "frxEURUSD", "frxGBPUSD", "frxUSDJPY",
]

# ── MULTIPLIER LEVERAGE — ⚠ UNAUDITED, see symbol_audit.py ─────────────
# DEFAULT_MULTIPLIER is used for every symbol below until you run
# symbol_audit.py against this account and fill in real, confirmed
# per-symbol ranges. Deliberately set LOW (not a guessed "typical" value)
# — Deriv rejects a buy outside a symbol's real allowed multiplier range,
# and a too-low guess just fails safely (no trade, no risk), whereas a
# too-high guess could either get rejected OR get accepted at more
# leverage than you'd have chosen deliberately. Do not raise this, and do
# not add per-symbol entries to MULTIPLIER_MAP, without a fresh
# symbol_audit.py run confirming the real range for YOUR account — ranges
# differ by jurisdiction/account type and can change. See
# SMC_ICT_MIGRATION_NOTES.md for the exact runbook.
DEFAULT_MULTIPLIER = 20
MULTIPLIER_MAP: dict = {}   # intentionally empty — every symbol falls
                             # through to DEFAULT_MULTIPLIER until audited

# ── STOP-LOSS — not used for this universe ──────────────────────────────
# DYNAMIC_STOP_LOSS_ENABLED / STOP_LOSS_MAP drove a stake-percentage stop
# for the old synthetic strategies. Every ICT signal always carries a real
# native_stop_price (the entry order block's own structural extreme), so
# bot_engine._execute() never falls through to a percentage-based stop for
# this universe — left False/empty rather than populated with guessed
# percentages that would never actually be read.
DYNAMIC_STOP_LOSS_ENABLED = False
STOP_LOSS_MAP: dict = {}
DEFAULT_STOP_LOSS_PCT = 0.30   # only reached if a signal somehow arrives
                                 # without native levels — should not happen;
                                 # see SignalEngine.evaluate()'s guard
TAKE_PROFIT_RATIO = 2.0         # same fallback-only role as above


# ══════════════════════════════════════════════════════════════
# TIMEFRAMES — sized for how ICT/SMC actually reads structure, not the
# old single-global-HTF-granularity setup this replaces.
# ══════════════════════════════════════════════════════════════
# HTF (bias + POI): 4-hour candles — where market structure (BOS/CHoCH)
#   and the order blocks/FVGs that matter sit. See ict_engine.py's module
#   docstring for why HTF collapses "bias" and "POI" into one timeframe
#   here rather than the 4 separate layers a manual ICT trader might use.
# MTF (confirmation): 15-minute candles — liquidity sweep + CHoCH/BOS
#   confirmation in the HTF bias direction.
# LTF (execution): 5-minute candles — the order block that caused the LTF
#   structure shift is the actual entry, with a tight structural stop.
HTF_GRANULARITY = 14400     # 4H
MTF_GRANULARITY = 900       # 15M
LTF_GRANULARITY = 300       # 5M

# Same granularities for every symbol in the ICT universe — Forex majors,
# gold, and the major commodities all behave similarly enough (deep,
# liquid, session-driven markets) that per-asset-class overrides aren't
# needed the way they might be for, say, a thin exotic pair. bot_engine.py
# still calls through _htf_gran()/_mtf_gran()/_ltf_gran() rather than
# reading these three constants directly, so a future override is a
# one-line change there if you ever want one.
FOREX_LTF_GRANULARITY = LTF_GRANULARITY
OTHER_LTF_GRANULARITY = LTF_GRANULARITY
FOREX_MTF_GRANULARITY = MTF_GRANULARITY
OTHER_MTF_GRANULARITY = MTF_GRANULARITY

# Bar counts kept in each CandlestickBuilder buffer. Needs to be enough
# history for meaningful swing/structure detection (ict_engine.py needs
# at least ~2*lookback+10 bars to do anything) with real room to spare —
# 150 HTF (4H) bars is ~25 days of structure, 150 MTF (15M) bars is
# ~1.5 days, 150 LTF (5M) bars is ~12.5 hours.
HTF_BARS = 150
MTF_BARS = 150
LTF_BARS = 150

# How often (seconds) bot_engine.py re-pulls HTF/MTF bars DIRECTLY from
# Deriv's own candle history (client.get_candles(), the same call used for
# startup seeding) rather than relying on the live tick-built rolling
# buffer. This is the primary defence against real markets' weekend/
# session-close gaps ever polluting the bars ict_engine.py reads bias/POI
# structure from — see candlestick_builder.CandlestickBuilder's
# max_gap_fill_bars for the (secondary) backstop on the tick-built path
# itself. LTF stays purely tick-built between HTF/MTF refreshes, for
# responsive execution timing.
HTF_MTF_REFRESH_FROM_BROKER_SECS = 900   # 15 min


# ══════════════════════════════════════════════════════════════
# ICT ENGINE TUNING — read by signal_engine.evaluate_ict() /
# ict_engine.analyze(). See ict_engine.py's module docstring for what
# each stage of the top-down sequence actually does.
# ══════════════════════════════════════════════════════════════
ICT_HTF_SWING_LOOKBACK = 3   # bars of confirmation either side of a swing
ICT_MTF_SWING_LOOKBACK = 2   # on HTF/MTF/LTF respectively — see
ICT_LTF_SWING_LOOKBACK = 2   # ict_engine.detect_swings()

ICT_MIN_RR_RATIO = 2.0        # reject any setup whose achievable
                                # reward:risk falls short of this

# Equal-highs/equal-lows clustering tolerance for liquidity-pool
# detection, as a fraction of price — tune wider for instruments with
# larger nominal price swings between "equal" levels (oil), tighter for
# tightly-quoted majors. "default" covers anything symbols.py's
# get_ict_asset_class() doesn't have a specific entry for.
ICT_LIQUIDITY_TOLERANCE_PCT = {
    "forex_major": 0.0006,
    "gold":        0.0008,
    "commodity":   0.0015,   # oil/silver move in larger relative
                               # increments than a Forex major
    "default":     0.0008,
}

# Killzones are computed from real UTC->America/New_York conversion (DST-
# correct) in ict_engine.active_killzone() — see its KILLZONES_ET table
# for the exact London/NY AM/NY PM windows. True = reject any signal
# outside all killzones outright (the guide's stronger recommendation for
# entry precision); False = still trade outside killzones, just without
# the killzone confluence-score bonus.
ICT_KILLZONE_HARD_FILTER = True


# ══════════════════════════════════════════════════════════════
# STAKE / RISK SIZING — unchanged generic mechanism. Real-money note: this
# universe trades real Forex/gold/commodity leverage via Multiplier
# contracts, not synthetic indices — MANUAL_STAKE_AMOUNT x
# MAX_CONCURRENT_TRADES is real currency exposure now, review both before
# going live rather than assuming settings tuned for synthetics still fit.
# ══════════════════════════════════════════════════════════════
MANUAL_STAKE_MODE   = True
MANUAL_STAKE_AMOUNT = 100.0

BASE_STAKE_PCT       = 0.005   # inactive while MANUAL_STAKE_MODE=True
MIN_STAKE            = 100
MAX_STAKE            = 1000.0
DAILY_LOSS_LIMIT_PCT = 0.06
DAILY_LOSS_PAUSE_MINS = 30

GLOBAL_CONSECUTIVE_LOSS_LIMIT = 4
GLOBAL_CONSECUTIVE_LOSS_PAUSE_MINS = 45

DRAWDOWN_DAMPENER_ENABLED   = True
DRAWDOWN_DAMPENER_START_PCT = 0.015
DRAWDOWN_DAMPENER_FULL_PCT  = 0.06
DRAWDOWN_DAMPENER_FLOOR     = 0.40

LOSS_STREAK_DAMPENER_ENABLED = True
LOSS_STREAK_DAMPENER_TABLE = [
    (2, 0.85),
    (3, 0.70),
    (4, 0.55),
]

# Win-streak stake scaling — off (1.0x = no-op at every tier). Turn on by
# raising the multipliers if you want compounding on win streaks.
PLS_WIN_THRESHOLDS  = [3,   5,   8,   12,  15  ]
PLS_WIN_MULTIPLIERS = [1.0, 1.0, 1.0, 1.0, 1.0]
PLS_WIN_EXTRA_SLOTS = [0,   0,   0,   0,   0   ]

KELLY_FRACTION_MULTIPLIER = 0.25   # dormant while MANUAL_STAKE_MODE=True

MAX_CONCURRENT_TRADES = 6

# Order-block "still relevant" window for smc_analyzer.py's dashboard
# context (SMCAnalyzer.__init__(ob_expiry_bars=...)) — purely cosmetic
# (dashboard display), not read by ict_engine.analyze()'s actual trading
# decision, which tracks OB mitigation directly instead of a bar-count
# expiry.
OB_EXPIRY_BARS = 50

# strategy_stats.py's underperforming-pair flag: below
# STRATEGY_WIN_RATE_FLOOR win rate, after at least
# STRATEGY_WIN_RATE_MIN_TRADES logged trades for that (strategy, symbol)
# pair, get_underperforming_pairs() flags it (dashboard/logging only —
# nothing currently auto-suspends a pair from this signal). 0.35 is a
# "clearly broken" bar, not a target: an ICT setup gated at
# ICT_MIN_RR_RATIO=2.0 only needs to win ~34% of the time to breakeven
# before costs, so this floor sits right at breakeven, not above it —
# tighten it once you have live data to judge against.
STRATEGY_WIN_RATE_FLOOR = 0.35
STRATEGY_WIN_RATE_MIN_TRADES = 30

# ── Aliases (bot_engine.py / risk_manager.py read these names) ──────────
RISK_PER_TRADE_PCT = BASE_STAKE_PCT
MAX_CONCURRENT     = MAX_CONCURRENT_TRADES
DAILY_LOSS_LIMIT   = DAILY_LOSS_LIMIT_PCT

ATR_PERIOD = 14   # read by signal_engine.compute_enriched_features()


# ══════════════════════════════════════════════════════════════
# SYMBOL SUSPENSION / SESSION GATING (symbol_manager.py)
# ══════════════════════════════════════════════════════════════
SYMBOL_MIN_GAP_MINS = 1
SESSION_LOSS_SUSPEND_LADDER_MINS = [70, 130, 190, 250]


# ── Per-family concurrency cap ───────────────────────────────────────────
# Coarse correlation grouping so the bot doesn't stack, say, 5 simultaneous
# USD-major Forex trades that are all really the same directional bet on
# the dollar. Mirrors symbols.get_ict_asset_class()'s 3-way split.
SYMBOL_FAMILY_MAP = {
    "frxXAUUSD": "metals",
    "frxXAGUSD": "metals",
    "frxUSOIL":  "energy",
    "frxUKOIL":  "energy",
    "frxEURUSD": "forex_major",
    "frxGBPUSD": "forex_major",
    "frxUSDJPY": "forex_major",
    "frxUSDCHF": "forex_major",
    "frxAUDUSD": "forex_major",
    "frxUSDCAD": "forex_major",
    "frxNZDUSD": "forex_major",
}
MAX_CONCURRENT_PER_FAMILY = 2


# ══════════════════════════════════════════════════════════════
# NEWS FILTER (news_filter.py)
# ══════════════════════════════════════════════════════════════
NEWS_BLOCK_MINUTES = 30
# Path to a JSON file of upcoming high-impact events you maintain — see
# news_filter.py's module docstring for the format. Not loaded
# automatically; call bot.news.load_events_from_json(config.NEWS_CALENDAR_JSON_PATH)
# yourself (e.g. on a daily timer) once you have a source for this you trust.
NEWS_CALENDAR_JSON_PATH = os.environ.get("NEWS_CALENDAR_JSON_PATH", "news_events.json")


# ══════════════════════════════════════════════════════════════
# CONTRACT / RECONCILIATION / MULTIPLIER MAX-HOLD
# ══════════════════════════════════════════════════════════════
CONTRACT_MAX_AGE_SECS     = 900
CONTRACT_FORCE_CLOSE_SECS = 1350
TRADE_DURATION_OVERRIDES = {}
TRADE_DURATION = 6
TRADE_DURATION_UNIT = "m"

RECONCILE_POLL_INTERVAL_SECS = 30
RECONCILE_MAX_SECS           = 1800

MULTIPLIER_MAX_HOLD_MINS = 30


# ══════════════════════════════════════════════════════════════
# TICK BUFFER / DEGRADED-SYMBOL RETRY / BUY-FAILURE CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════
TICK_BUFFER_MAXLEN = 200
TICK_RESUBSCRIBE_RETRY_SECS = 30

BUY_FAILURE_CIRCUIT_BREAKER_THRESHOLD    = 5
BUY_FAILURE_CIRCUIT_BREAKER_SUSPEND_MINS = 15


# ══════════════════════════════════════════════════════════════
# SCANNING / RATE LIMITING
# ══════════════════════════════════════════════════════════════
SCAN_CYCLE_SLEEP  = 1
INIT_BATCH_SIZE   = 8
INIT_BATCH_DELAY  = 0.3

BUY_REQUEST_DELAY_SECS = 3.0
MAX_BUY_PER_SECOND     = 3


# ══════════════════════════════════════════════════════════════
# RENDER REDEPLOY
# ══════════════════════════════════════════════════════════════
RENDER_DEPLOY_HOOK_URL = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")
REDEPLOY_EVERY_N_CYCLES = 999999
SETTLE_WAIT_SECS = 15
REDEPLOY_TIMEZONE = "Africa/Nairobi"
REDEPLOY_INTERVAL_HOURS = 11 / 60
DRAIN_MAX_SECS = 1800


# ══════════════════════════════════════════════════════════════
# ENSEMBLE VOTING — dormant with a single strategy (nothing left for a
# second strategy to agree WITH), kept as infrastructure in case a second
# independent ICT-variant evaluator is ever added.
# ══════════════════════════════════════════════════════════════
ENSEMBLE_MODE = False
ENSEMBLE_AGREEMENT_WINDOW_SECS = 60
ENSEMBLE_MIN_STRATEGIES_AGREEING = 2


# ══════════════════════════════════════════════════════════════
# META-LABELING (meta_labeling.py) — the enriched-feature EV-model/
# retrain machinery is generic (works off compute_enriched_features(),
# unrelated to which strategy produced the trade) and kept. The
# TAKE/INVERT Bayesian bandit config is NOT kept — see this file's module
# docstring for why (confirmed dead/disconnected from execution even
# before this pivot).
# ══════════════════════════════════════════════════════════════
META_LABEL_MIN_TRADES      = 200
META_LABEL_RETRAIN_EVERY_N = 100
META_LABEL_EV_MARGIN       = 0.03


# ══════════════════════════════════════════════════════════════
# ADAPTIVE EXIT ENGINE (exit_engine.py) — generic, Multiplier-contract-
# only management layer (trailing stop as profit grows, early close on
# profit decay, capped early loss). Unchanged mechanism; EXIT_ENGINE_SYMBOLS
# now points at the ICT universe instead of the old synthetic Multiplier
# symbols.
# ══════════════════════════════════════════════════════════════
EXIT_ENGINE_ENABLED = True
EXIT_ENGINE_SYMBOLS = list(MULTIPLIER_SYMBOLS)

EXIT_ARM_PROFIT_FRACTION    = 0.15
EXIT_TRAIL_LOCK_FRACTION    = 0.75
EXIT_DECAY_CLOSE_FRACTION   = 0.20
EXIT_POLL_INTERVAL_SECS     = 15

EXIT_LOSS_CAP_ENABLED       = True
EXIT_LOSS_CAP_FRACTION      = 0.25
EXIT_LOSS_CAP_GRACE_SECS    = 20

EXIT_ML_ENABLED             = True
EXIT_ML_MODEL_PATH          = "exit_model.joblib"
EXIT_ML_FEATURE_WINDOW      = 5
EXIT_ML_MIN_CONFIDENCE      = 0.60

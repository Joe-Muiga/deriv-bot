import os

# ── GENERAL ───────────────────────────────────────────────────
LOG_LEVEL = "INFO"
DEBUG     = False
VERSION   = "1.1.0"

# ── DERIV API ─────────────────────────────────────────────────
DERIV_API_TOKEN = os.environ.get("DERIV_API_TOKEN", "")
DERIV_APP_ID    = os.environ.get("DERIV_APP_ID", "1089")

# ── SERVER ────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 10000))
SELF_URL = os.environ.get("SELF_URL", os.environ.get("RENDER_EXTERNAL_URL", ""))
KEEP_ALIVE_INTERVAL = 600  # seconds between self-ping requests

# ── ALL DERIV SYNTHETIC INDICES ──────────────────────────────

# Standard Volatility (2s tick)
VOLATILITY_STANDARD = [
    "R_10","R_25","R_50","R_75","R_100",
]

# 1-Second Volatility (faster tick)
# 1HZ150V / 1HZ200V / 1HZ250V removed — confirmed OfferingsInvalidSymbol
# by a real contracts_for audit (symbol_audit.py, 2026-07-31). These were
# added in an earlier pass "by pattern" (same family as 1HZ10V-100V, and
# already had MULTIPLIER_MAP/STOP_LOSS_MAP entries) without empirical
# verification, and were silently failing every buy attempt — this was a
# live bug in the currently-active mean_reversion strategy (they were
# already in RISE_FALL_SYMBOLS / MEAN_REVERSION_SYMBOLS), not a dormant
# one. Do not re-add without a fresh audit confirming they exist on this
# account.
VOLATILITY_1S = [
    "1HZ10V","1HZ25V","1HZ50V",
    "1HZ75V","1HZ100V",
]

# Boom & Crash
# NOTE: Removed from trading — earlier note claimed Boom/Crash don't
# support CALL/PUT Rise/Fall (OfferingsValidationError). The 2026-07-31
# audit only checked these for Multiplier support (confirmed: BOOM500/
# CRASH500 x100-400, BOOM1000/CRASH1000 x100-500 — see MULTIPLIER_MAP) and
# didn't re-test CALL/PUT specifically. Given the same audit run found the
# near-identical "OfferingsValidationError" claim for Jump indices was
# stale (caused by a currency-param bug in contracts_for(), now fixed),
# don't assume this CALL/PUT claim is still accurate either way — it needs
# an explicit re-check, not a re-assertion of the old conclusion. Blocked
# on buy_multiplier() for the confirmed Multiplier path regardless.
BOOM_CRASH = []

# Step Index
STEP = ["stpRNG"]

# Jump Indices
# Old note here claimed Jump indices don't support CALL/PUT
# (OfferingsValidationError) — a real contracts_for audit (symbol_audit.py,
# 2026-07-31) contradicts that: JD10-JD100 all confirmed to support
# CALL/PUT Rise/Fall, plus Multipliers and digit contracts. The earlier
# failure was most likely the currency-param bug in contracts_for() that
# was fixed alongside this audit run, not an actual product limitation —
# treat the old "OfferingsValidationError" conclusion as stale.
# Confirmed Multiplier ranges (for the later buy_multiplier() wiring pass —
# NOT used yet; MULTIPLIER_SYMBOLS below intentionally doesn't include
# these until buy_multiplier() exists in deriv_client.py):
#   JD10: x100-x1000   JD25: x50-x500   JD50: x20-x200
#   JD75: x15-x150     JD100: x10-x100
JUMP = ["JD10", "JD25", "JD50", "JD75", "JD100"]

# Range Break
# There is no confirmed true Range Break product on this account. RDBULL/
# RDBEAR were previously (wrongly) filed under this category with a
# "MT5-only, not reachable" note — a real contracts_for audit (2026-07-31)
# disproves that (see the Bear/Bull section below, where they've been
# moved). RANGE_BREAK stays empty/disabled unless a genuine Range Break
# symbol code is ever confirmed for this account.
RANGE_BREAK = []
RANGE_BREAK_ENABLED = False

# Drift Switch
DRIFT = ["DSHIFT10","DSHIFT20","DSHIFT30"]

# ── BEAR/BULL ("DAILY RESET") MARKET SYMBOLS ─────────────────
# Confirmed via a real contracts_for audit (symbol_audit.py, 2026-07-31):
# RDBEAR and RDBULL both offer CALL/PUT, Touch/No Touch, digit contracts,
# and Range/Up-or-Down — no MULTUP/MULTDOWN. So they're reachable via this
# bot's existing Rise/Fall (CALL/PUT) path right now, with no dependency
# on buy_multiplier() being built. Routed to evaluate_trend_shift() via
# BEAR_BULL_SYMBOLS below (see signal_engine.py's dispatcher) and also
# added to RISE_FALL_SYMBOLS so they're actually initialised/scanned
# (ALL_TRADE_SYMBOLS is derived from RISE_FALL_SYMBOLS — see note below).
BEAR_BULL_SYMBOLS = ["RDBEAR", "RDBULL"]
BEAR_BULL_TREND_SHIFT_MINS = 20     # 10 / 20 / 30 — unchanged default

# Symbols confirmed via contracts_for to support CALL/PUT Rise/Fall on
# this account. Last empirically verified: symbol_audit.py run, 2026-07-31.
#   - 1HZ150V/1HZ200V/1HZ250V removed — confirmed OfferingsInvalidSymbol,
#     see VOLATILITY_1S note above.
#   - JD10/JD25/JD50/JD75/JD100 added — confirmed CALL/PUT support; also
#     routed to JUMP_BUILDUP_SYMBOLS below for evaluate_jump_buildup().
#   - RDBEAR/RDBULL added — confirmed CALL/PUT support (previously wrongly
#     assumed MT5-only); also routed to BEAR_BULL_SYMBOLS below for
#     evaluate_trend_shift().
# Still NOT verified / not added: BOOM300N, CRASH300N (came back
# OfferingsInvalidSymbol — likely a naming bug, config.py's MULTIPLIER_MAP
# uses "BOOM300"/"CRASH300" with no "N" suffix; re-run the audit against
# the un-suffixed names before adding), DSHIFT10/20/30 (not in symbols.py's
# SYNTHETIC list at all, so never queried — add them there first).
RISE_FALL_SYMBOLS = [
    "R_10","R_25","R_50","R_75","R_100",
    "1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V",
    "stpRNG",
    "JD10","JD25","JD50","JD75","JD100",
    "RDBEAR","RDBULL",
]

# Digit (Match/Differ/Over/Under/Even/Odd) contracts are not used by this bot —
# only CALL/PUT Rise/Fall is traded — so this stays empty. signal_engine.py
# checks `if symbol in config.DIGIT_SYMBOLS` to route digit-specific logic;
# an empty list means that branch is always skipped, as intended.
DIGIT_SYMBOLS = []

# ── STRATEGY ROUTING (signal_engine.py) ──────────────────────
# Every traded symbol is routed to exactly one strategy evaluator.
MEAN_REVERSION_SYMBOLS = VOLATILITY_STANDARD + VOLATILITY_1S  # all 7 vol indices
RANGE_BREAK_SYMBOLS    = []                                      # disabled — see RANGE_BREAK note
BOOM_CRASH_SYMBOLS     = BOOM_CRASH                              # empty — blocked on buy_multiplier()
STEP_SYMBOLS           = STEP                                    # stpRNG
JUMP_BUILDUP_SYMBOLS   = JUMP                                    # JD10-JD100 — see JUMP note above
JUMP_SYMBOLS            = JUMP                                    # alias — symbol_manager.py's
                                                                   # is_in_session() reads this name
# BEAR_BULL_SYMBOLS is defined earlier, in its own section above.
# DIGIT_SYMBOLS / DIGIT_PARITY_SYMBOLS / DRIFT_FADE_SYMBOLS intentionally
# not defined here — see the notes by DIGIT_SYMBOLS above (deliberately
# off) and DRIFT above (DSHIFT10/20/30 never audited — not in symbols.py).

# bot_engine._init_all_symbols() reads ALL_TRADE_SYMBOLS (falling back to
# ALL_SYMBOLS) as the ONLY list of symbols that ever get initialised or
# scanned — a symbol missing from here never runs through ANY strategy,
# regardless of being listed in JUMP_BUILDUP_SYMBOLS / BEAR_BULL_SYMBOLS /
# etc. This used to just alias RISE_FALL_SYMBOLS, which silently meant
# nothing outside plain Rise/Fall could ever be scanned even if wired
# elsewhere. Now a real union of every populated strategy list, so newly
# routed Jump/Bear-Bull symbols actually get scanned.
# DIGIT_PARITY_SYMBOLS / DRIFT_FADE_SYMBOLS aren't included below because
# they aren't defined anywhere in this file yet (both currently empty via
# signal_engine.py's getattr(..., []) fallback) — add them to this union
# too, the day either one gets a real symbol list.
ALL_TRADE_SYMBOLS = list(dict.fromkeys(
    RISE_FALL_SYMBOLS + MEAN_REVERSION_SYMBOLS + RANGE_BREAK_SYMBOLS
    + BOOM_CRASH_SYMBOLS + STEP_SYMBOLS + JUMP_BUILDUP_SYMBOLS
    + BEAR_BULL_SYMBOLS + DIGIT_SYMBOLS
))
ALL_SYMBOLS        = ALL_TRADE_SYMBOLS
VOLATILITY_SYMBOLS = ALL_TRADE_SYMBOLS  # alias for compatibility with bot_engine.py

# ── MULTIPLIER SETTINGS ──────────────────────────────────────
# Higher volatility = higher multiplier potential
MULTIPLIER_MAP = {
    # Low volatility — moderate multiplier
    "R_10":    100, "1HZ10V":  100,
    "R_25":    200, "1HZ25V":  200,
    # Medium volatility
    "R_50":    300, "1HZ50V":  300,
    "R_75":    500, "1HZ75V":  500,
    # High volatility — maximum multiplier
    "R_100":   500, "1HZ100V": 500,
    # 1HZ150V/1HZ200V/1HZ250V entries removed — confirmed invalid symbols,
    # see VOLATILITY_1S note above.
    # Boom/Crash — moderate (spike risk). Still unused (BOOM_CRASH_SYMBOLS
    # is empty, blocked on buy_multiplier()) — values below predate the
    # 2026-07-31 audit and haven't been reconciled against confirmed real
    # ranges (BOOM500/CRASH500: x100-400, BOOM1000/CRASH1000: x100-500) —
    # revisit when BOOM_CRASH_SYMBOLS actually gets populated.
    "BOOM150": 100, "BOOM300": 100,
    "BOOM500": 100, "BOOM1000":100,
    "CRASH150":100, "CRASH300":100,
    "CRASH500":100, "CRASH1000":100,
    # Others
    "stpRNG":  200,
    # JD10-JD100: not yet in MULTIPLIER_SYMBOLS (Rise/Fall is used for
    # these right now, see RISE_FALL_SYMBOLS above) — values below predate
    # the audit; confirmed real ranges are noted by the JUMP list above
    # (JD10 x100-1000, JD25 x50-500, JD50 x20-200, JD75 x15-150,
    # JD100 x10-100) for whenever Multiplier routing is added for these.
    "JD10":    100, "JD25":    200,
    "JD50":    300, "JD75":    400,
    "JD100":   500,
    # RDBULL/RDBEAR entries removed — audit confirmed no MULTUP/MULTDOWN
    # support on either; they're traded via Rise/Fall (CALL/PUT) instead,
    # see BEAR_BULL_SYMBOLS above. STOP_LOSS_MAP has no entry for them
    # either, so they fall back to DEFAULT_STOP_LOSS_PCT.
    "DSHIFT10":200, "DSHIFT20":200,
    "DSHIFT30":200,
}
DEFAULT_MULTIPLIER = 100

# ── STOP LOSS AND TAKE PROFIT (% of stake) ──────────────────
# Stop loss as % of stake — caps maximum loss per trade
STOP_LOSS_MAP = {
    # Low vol — tighter SL
    "R_10":    30.0,  "1HZ10V":  30.0,
    "R_25":    40.0,  "1HZ25V":  40.0,
    # Medium vol
    "R_50":    50.0,  "1HZ50V":  50.0,
    "R_75":    60.0,  "1HZ75V":  60.0,
    # High vol — wider SL to avoid noise
    "R_100":   70.0,  "1HZ100V": 70.0,
    # 1HZ150V/1HZ200V/1HZ250V entries removed — confirmed invalid symbols,
    # see VOLATILITY_1S note above.
    # Boom/Crash — wide SL due to spikes
    "BOOM150": 50.0,  "BOOM300": 50.0,
    "BOOM500": 50.0,  "BOOM1000":50.0,
    "CRASH150":50.0,  "CRASH300":50.0,
    "CRASH500":50.0,  "CRASH1000":50.0,
}
DEFAULT_STOP_LOSS_PCT = 50.0

# Take profit = 2x stop loss (2:1 RR minimum)
TAKE_PROFIT_RATIO = 2.0

# ── STAKE SETTINGS ───────────────────────────────────────────
BASE_STAKE_PCT       = 0.005   # 0.5% of current balance per trade — this
                                # IS the compounding: stake grows/shrinks
                                # automatically as balance grows/shrinks.
MIN_STAKE            = 0.35    # safety floor — never stake less than this
MAX_STAKE            = 1000.0  # safety backstop only, not the everyday
                                # driver — was previously == MIN_STAKE,
                                # which silently capped every trade at
                                # $0.35 regardless of balance or the 0.5%
                                # calculation above. Adjust if you want a
                                # tighter per-trade ceiling.
DAILY_LOSS_LIMIT_PCT = 0.15   # NOTE: value is 15%, comment below said 20% — see flags in reply
DAILY_LOSS_PAUSE_MINS = 30

# ── AGGRESSIVE COMPOUNDING ───────────────────────────────────
# Disabled per user request — stake no longer scales up on win streaks.
# Multipliers all set to 1.0 so PLS tier lookups (wherever risk_manager.py
# applies them) are a no-op; stake stays flat regardless of streak length.
PLS_WIN_THRESHOLDS  = [3,   5,   8,   12,  15  ]
PLS_WIN_MULTIPLIERS = [1.0, 1.0, 1.0, 1.0, 1.0]
PLS_WIN_EXTRA_SLOTS = [0,   0,   0,   0,   0   ]

# ── CONCURRENT TRADES ────────────────────────────────────────
MAX_CONCURRENT_TRADES = 30

# ── TIMEFRAMES ───────────────────────────────────────────────
HTF_GRANULARITY   = 3600   # 1H
MTF_GRANULARITY   = 300    # 5M
LTF_GRANULARITY   = 60     # 1M
HTF_BARS          = 100
MTF_BARS          = 50
LTF_BARS          = 30

# ── SIGNAL SETTINGS ──────────────────────────────────────────
MIN_SIGNAL_SCORE       = 0.68
MIN_STRATEGY_AGREEMENT = 4

# SMC parameters
OB_LOOKBACK            = 50
FVG_MIN_ATR            = 0.5
SWEEP_LOOKBACK         = 20
SWING_LOOKBACK         = 5
FIB_LEVELS             = [0.382, 0.5, 0.618, 0.786]
FIB_TOLERANCE          = 0.1
EMA_FAST              = 8
EMA_SLOW              = 21
EMA_TREND             = 50
RSI_PERIOD            = 14
RSI_OVERBOUGHT        = 70
RSI_OVERSOLD          = 30
MOMENTUM_LOOKBACK     = 10
ATR_PERIOD            = 14
BREAKOUT_ATR_MULT     = 1.5

# ── CONTRACT SETTINGS ────────────────────────────────────────
# Multiplier contracts — keep short to avoid funding fees
MAX_TRADE_OPEN_MINS   = 30   # force close at 30 min
CHECK_TRADE_MINS      = 20   # check at 20 min
CONTRACT_CHECK_SECS   = 1200
CONTRACT_TIMEOUT_SECS = 1800

# ── SYMBOL SUSPENSION (minutes) ──────────────────────────────
SYMBOL_WIN_SUSPEND_MINS   = 20
SYMBOL_LOSS_SUSPEND_MINS  = 55
SYMBOL_MIN_GAP_MINS       = 1
SYMBOL_SESSION_BAN_LOSSES = 4

# ── RAW TICK BUFFER (feeds tick-based evaluators via evaluate(ticks=...)) ──
TICK_BUFFER_MAXLEN = 200

# ── DEGRADED-SYMBOL TICK-SUBSCRIPTION RETRY ──────────────────
TICK_RESUBSCRIBE_RETRY_SECS = 30

# ── BUY-FAILURE CIRCUIT BREAKER ──────────────────────────────
BUY_FAILURE_CIRCUIT_BREAKER_THRESHOLD    = 5
BUY_FAILURE_CIRCUIT_BREAKER_SUSPEND_MINS = 15

# ── SCANNING ────────────────────────────────────────────────
SCAN_CYCLE_SLEEP       = 1
INIT_BATCH_SIZE        = 8
INIT_BATCH_DELAY       = 0.3
PRIORITY_SYMBOLS = [
    "R_75","R_100","1HZ75V","1HZ100V",
    "R_50","R_25",
]

# ── RATE LIMITING ────────────────────────────────────────────
BUY_REQUEST_DELAY_SECS = 3.0
MAX_BUY_PER_SECOND     = 3

# ── RENDER ──────────────────────────────────────────────────
RENDER_DEPLOY_HOOK_URL = os.environ.get(
    "RENDER_DEPLOY_HOOK_URL","")
# REDEPLOY_EVERY_N_CYCLES previously = 8. With SETTLE_WAIT_SECS defaulting to
# 15s (bot_engine._settle_loop), that was an 8*15=120s cycle-based redeploy —
# fighting restart_scheduler.py's timer and redeploying roughly every 2
# minutes instead of every 2 hours. Set high enough that it never fires on
# its own; restart_scheduler.py's fixed 2-hour timer is now the only
# redeploy trigger. Lower this back down only if you deliberately want a
# SECOND, settle-count-based redeploy path in addition to the timer.
REDEPLOY_EVERY_N_CYCLES = 999999
SETTLE_WAIT_SECS = 15

# ── ALIASES (required by bot_engine.py / risk_manager.py) ────
RISK_PER_TRADE_PCT = BASE_STAKE_PCT          # alias
MAX_CONCURRENT     = 20               # alias — NOTE: mismatched with MAX_CONCURRENT_TRADES=30, see flags
DAILY_LOSS_LIMIT   = DAILY_LOSS_LIMIT_PCT    # alias

# ── ADDITIONAL SIGNAL/RISK SETTINGS ──────────────────────────
MIN_MODULES_FOR_SIGNAL     = 3
MIN_INDICATOR_VOTES        = 3
OB_EXPIRY_BARS             = 100
NEWS_BLOCK_MINUTES         = 60
FOREX_LTF_GRANULARITY      = 900
OTHER_LTF_GRANULARITY      = 60
MIN_SIGNAL_PROBABILITY     = 1.8
MIN_STRENGTH_REPEAT_SYMBOL = 3

# ── DERIV WEBSOCKET ───────────────────────────────────────────
DERIV_WS_URL : str = os.environ.get(
    "DERIV_WS_URL",
    f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
)

# ── SIGNAL GENERATION GATES (additional) ─────────────────────
MIN_SCORE                  = 2.0
MIN_CONFLUENCE             = 2
MIN_MODULE_STRENGTH        = 2
MIN_MODULE_STRENGTH_NORMAL = 2
MIN_CONFIDENCE_NORMAL      = 5
MIN_CONFIDENCE_FOR_PARTIAL = 5

# ── SESSION TIMING — disabled for 24/7 synthetics ────────────
DEAD_ZONE_START_UTC  = 0
DEAD_ZONE_END_UTC    = 5
BOOM500_PRIME_START  = 7
BOOM500_PRIME_END    = 12
TRADE_DURATION = 14
TRADE_DURATION_UNIT = "m"

# ══════════════════════════════════════════════════════════════
# NEW STRATEGY CONFIG (added in this pass — none of these are
# wired into ALL_SYMBOLS / signal_engine.py routing yet; they are
# config surfaces for strategies you're building incrementally)
# ══════════════════════════════════════════════════════════════

# ── DIGIT STRATEGY (Over/Under) ───────────────────────────────
# When True, a Digit Over/Under signal must be confirmed by BOTH an
# indicator-based read AND a statistical digit-frequency read before
# it fires. DIGIT_SYMBOLS is still empty above, so this is inert
# until you populate that list.
DIGIT_HYBRID_MODE = False

# ── ACCUMULATOR SETTINGS ──────────────────────────────────────
ACCU_GROWTH_RATE_MIN = 1.0   # percent, per-tick growth rate floor
ACCU_GROWTH_RATE_MAX = 5.0   # percent, per-tick growth rate ceiling
# Fraction of a symbol's historical average in-range tick survival at
# which to take profit early instead of holding to knockout.
# e.g. 0.7 = exit once you've captured 70% of the typical survival length.
ACCU_EXIT_FRACTION = 0.7

# ── STRATEGY PERFORMANCE MONITORING ───────────────────────────
# Once a (strategy, symbol) pair has 100+ logged trades, flag it as
# underperforming if its win rate falls below this floor.
STRATEGY_WIN_RATE_FLOOR = 0.55
STRATEGY_WIN_RATE_MIN_TRADES = 100

# ── META-LABELING (future ML filter) ──────────────────────────
META_LABEL_MIN_TRADES      = 200   # trades required before the filter is trusted
META_LABEL_RETRAIN_EVERY_N = 100   # retrain cadence, in newly logged trades

# ── POSITION SIZING (Kelly) ───────────────────────────────────
# Conservative multiplier applied to full Kelly-optimal sizing.
# 0.25 = quarter-Kelly.
KELLY_FRACTION_MULTIPLIER = 0.25

# ── ENSEMBLE MODE ──────────────────────────────────────────────
# When True, requires 2+ independent strategies to agree within the
# agreement window before a signal fires.
ENSEMBLE_MODE = False
ENSEMBLE_AGREEMENT_WINDOW_SECS = 60
ENSEMBLE_MIN_STRATEGIES_AGREEING = 2

# ── SESSION / DAY-OF-WEEK SCORE WEIGHTING ─────────────────────
# Multiplier applied to signal score based on symbol category, UTC
# hour, and day of week. Range is 0.8-1.2 by convention (0.8 = dampen,
# 1.2 = boost, 1.0 = neutral/no adjustment).
#
# `days` uses Python's datetime.weekday() convention:
#   Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
# `days: None` means "applies every day". `hours_utc` is an inclusive
# (start, end) 24h UTC range; `hours_utc: None` means "applies all hours".
#
# NOTE: Boom/Crash symbols are currently NOT traded by this bot
# (BOOM_CRASH = [] above, blocked on buy_multiplier() being built). Jump
# indices (JD10-JD100) ARE now traded via Rise/Fall as of the 2026-07-31
# audit, but get_symbol_class() still returns a generic category for
# them (check symbols.py — there's no "jump" branch in get_symbol_class()
# yet), so these table entries have no effect until that's added. These
# remain config-only placeholders for now.
SESSION_DOW_WEIGHT_TABLE = {
    "BOOM600_CRASH900": {
        "hours_utc": (14, 20),   # boosted 14:00-20:00 UTC
        "days": None,             # every day
        "multiplier": 1.2,
    },
    "BOOM300N_CRASH300N": {
        "hours_utc": None,
        "days": [6],               # Sunday
        "multiplier": 1.15,
    },
    "JUMP50": {
        "hours_utc": (11, 13),   # around 12:00 UTC
        "days": [5],               # Saturday
        "multiplier": 1.15,
    },
}
SESSION_DOW_WEIGHT_DEFAULT = 1.0  # applied when no table entry matches

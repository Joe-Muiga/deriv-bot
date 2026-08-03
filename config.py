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
# ACTIVATED: BOOM500/BOOM1000/CRASH500/CRASH1000 confirmed MULTUP/MULTDOWN
# by the 2026-07-31 audit (x100-400 / x100-500 — see MULTIPLIER_MAP), all
# four already sit in MULTIPLIER_SYMBOLS, and buy_multiplier() exists in
# deriv_client.py. That was everything needed to trade them — this was the
# last deliberate switch (previously left empty on purpose, see git
# history / prior comment). Routes to evaluate_boom_crash() via
# BOOM_CRASH_SYMBOLS below, dispatched to buy_multiplier() by bot_engine.py.
# CALL/PUT support for these four was never re-tested post the
# currency-param fix — doesn't matter here since they only trade via
# Multipliers, but don't assume Rise/Fall works for them without a fresh
# check if that path is ever wanted.
# NOT included: BOOM300N/CRASH300N (OfferingsInvalidSymbol — likely a
# naming bug, config's un-suffixed BOOM300/CRASH300 was never queried) and
# BOOM150/CRASH150 (never queried at all). Re-run the audit against the
# un-suffixed codes before adding any of the four.
BOOM_CRASH = ["BOOM500", "BOOM1000", "CRASH500", "CRASH1000"]

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

# Implementation Brief v5 / A5 — CONFIRMED bug, not just a flagged
# assumption. bot_engine.py's _init_all_symbols() seeds every symbol
# (via _init_data()) with LTF_BARS=30 bars regardless of category, and
# the CandlestickBuilder is capped at ltf_bars + 20 = 50 bars. But
# evaluate_trend_shift() (signal_engine.py) requires
# min_bars = max(EMA_TREND=50, RSI_PERIOD=14, ATR_PERIOD=14) + 1 = 51
# bars before it will evaluate anything — one bar more than the cap can
# ever hold. TREND_SHIFT has been returning NONE_RESULT on every single
# call for RDBEAR/RDBULL, contributing zero trades regardless of market
# conditions. This override raises the seed (and therefore the cap) for
# BEAR_BULL_SYMBOLS specifically — see the ltf_bars override in
# bot_engine.py's _init_all_symbols() — without touching LTF_BARS=30 for
# every other category.
BEAR_BULL_LTF_BARS = 60

# Implementation Brief v3, finding #4: each Daily Reset index holds ONE
# fixed characteristic trend for its entire 24h cycle (Bull always up,
# Bear always down, per Deriv's own product description) — this is a
# static fact, not something signal_engine.py should derive from EMAs or
# alternate at each reset. evaluate_trend_shift() reads this map directly;
# BEAR_BULL_TREND_SHIFT_MINS above is used only to gate entry timing
# (skip trading until the post-reset window closes), never to pick a side.
BEAR_BULL_DIRECTION = {"RDBULL": "LONG", "RDBEAR": "SHORT"}

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

# Digit (Match/Differ/Over/Under/Even/Odd) — evaluate_digit() is built and
# waiting for symbols, but stays empty this pass. Two separate reasons:
#   1. The 2026-07-31 audit only confirmed digit-contract support for
#      JD10-JD100 and RDBEAR/RDBULL (bundled under "digit contracts" in the
#      contracts_for results) — it never tested R_10-R_100, 1HZ10V-100V, or
#      stpRNG, which are the symbols you'd actually want a digit strategy
#      on. Don't add those on a guess; re-run the audit against them
#      specifically.
#   2. Even the confirmed ones (JD*, RDBEAR/RDBULL) can't go here anyway
#      without a decision: they're already committed to JUMP_BUILDUP_SYMBOLS
#      and BEAR_BULL_SYMBOLS respectively, and "every traded symbol routes
#      to exactly one strategy evaluator" (see STRATEGY ROUTING below) — so
#      adding them to DIGIT_SYMBOLS too would double-route them. That's a
#      strategy call, not a data-confirmation one; get explicit sign-off
#      before reassigning a symbol off its current strategy.
# signal_engine.py checks `if symbol in config.DIGIT_SYMBOLS`; empty means
# that branch is always skipped.
DIGIT_SYMBOLS = []

# ── STRATEGY ROUTING (signal_engine.py) ──────────────────────
# Every traded symbol is routed to exactly one strategy evaluator.
MEAN_REVERSION_SYMBOLS = VOLATILITY_STANDARD + VOLATILITY_1S  # all 7 vol indices
RANGE_BREAK_SYMBOLS    = []                                      # disabled — no genuine Range Break
                                                                   # symbol has ever been confirmed on
                                                                   # this account (RDBEAR/RDBULL are
                                                                   # Bear/Bull, not Range Break — see
                                                                   # RANGE_BREAK note above)
BOOM_CRASH_SYMBOLS     = BOOM_CRASH                              # BOOM500/BOOM1000/CRASH500/CRASH1000
                                                                   # — ACTIVATED this pass, see
                                                                   # BOOM_CRASH note above
STEP_SYMBOLS           = STEP                                    # stpRNG
JUMP_BUILDUP_SYMBOLS   = JUMP                                    # JD10-JD100 — see JUMP note above
JUMP_SYMBOLS            = JUMP                                    # alias — symbol_manager.py's
                                                                   # is_in_session() reads this name
DIGIT_PARITY_SYMBOLS   = []                                      # evaluate_digit_parity() built, no
                                                                   # symbols wired — same two blockers
                                                                   # as DIGIT_SYMBOLS above (audit
                                                                   # never tested the actually-free
                                                                   # candidates for digit contracts;
                                                                   # the confirmed ones are already
                                                                   # claimed by other strategies)
DRIFT_FADE_SYMBOLS     = []                                      # evaluate_drift_fade() built, no
                                                                   # symbols wired — DSHIFT10/20/30
                                                                   # aren't in symbols.py's SYNTHETIC
                                                                   # list, so the 2026-07-31 audit
                                                                   # never queried them at all. Add
                                                                   # them there, re-run the audit,
                                                                   # then populate this.
# BEAR_BULL_SYMBOLS is defined earlier, in its own section above (RDBEAR/
# RDBULL — routed via Rise/Fall, NOT Multipliers; audit confirmed no
# MULTUP/MULTDOWN support on either, so despite this task's original
# assumption that Bear/Bull strategies only reach symbols via
# buy_multiplier(), these two only work through the CALL/PUT path — leave
# them exactly as already wired above).

# bot_engine.py's _execute() checks `if symbol in config.MULTIPLIER_SYMBOLS`
# to decide whether a symbol routes to buy_multiplier() instead of
# buy_contract(). buy_multiplier() now exists in deriv_client.py and is
# callable. Populated here ONLY with symbols that have a real, confirmed
# contracts_for audit result AND no conflicting existing route:
#   BOOM500/BOOM1000/CRASH500/CRASH1000 — confirmed MULTUP/MULTDOWN support
#   (x100-400 / x100-500 respectively) by the 2026-07-31 audit, not traded
#   any other way (BOOM_CRASH_SYMBOLS is now populated too — see STRATEGY
#   ROUTING above — so these are fully wired end-to-end: MULTIPLIER_SYMBOLS
#   routes them to buy_multiplier(), BOOM_CRASH_SYMBOLS gets them into
#   ALL_TRADE_SYMBOLS and evaluate_boom_crash()).
#
# Deliberately left OUT, each for a different reason — do not add without
# resolving the specific blocker noted:
#   JD10/JD25/JD50/JD75/JD100 — audit confirmed these DO support
#     Multipliers, but they're also confirmed for CALL/PUT and are
#     ALREADY live via RISE_FALL_SYMBOLS today. Adding them here would
#     silently reroute them off a currently-working Rise/Fall path onto
#     Multipliers (_execute()'s branch is if/else, not both) — that's a
#     strategy decision, not just a data-confirmation one. Get explicit
#     sign-off before switching.
#   DSHIFT10/DSHIFT20/DSHIFT30 — never audited at all (not in symbols.py's
#     SYNTHETIC list, so symbol_audit.py never queried them). The values
#     already sitting in MULTIPLIER_MAP for these predate any real check.
#     Add DSHIFT10/20/30 to symbols.py, re-run the audit, then reconsider.
#   BOOM300/CRASH300/BOOM150/CRASH150 (un-suffixed) — audit only tested
#     BOOM300N/CRASH300N (OfferingsInvalidSymbol — believed to be a naming
#     bug) and never queried BOOM150/CRASH150 at all. Re-run the audit
#     against the correct un-suffixed codes before adding.
#   RDBEAR/RDBULL — audit explicitly confirmed NO MULTUP/MULTDOWN support
#     on either. Will never belong here; they trade via Rise/Fall only.
#
MULTIPLIER_SYMBOLS = ["BOOM500", "BOOM1000", "CRASH500", "CRASH1000"]

# bot_engine._init_all_symbols() reads ALL_TRADE_SYMBOLS (falling back to
# ALL_SYMBOLS) as the ONLY list of symbols that ever get initialised or
# scanned — a symbol missing from here never runs through ANY strategy,
# regardless of being listed in JUMP_BUILDUP_SYMBOLS / BEAR_BULL_SYMBOLS /
# etc. This used to just alias RISE_FALL_SYMBOLS, which silently meant
# nothing outside plain Rise/Fall could ever be scanned even if wired
# elsewhere. Now a real union of every populated strategy list, so newly
# routed Jump/Bear-Bull symbols actually get scanned.
# DIGIT_PARITY_SYMBOLS / DRIFT_FADE_SYMBOLS are now explicitly defined
# above (both still [] — see STRATEGY ROUTING section) and included below
# so nothing needs to change here the day either one gets real symbols.
ALL_TRADE_SYMBOLS = list(dict.fromkeys(
    RISE_FALL_SYMBOLS + MEAN_REVERSION_SYMBOLS + RANGE_BREAK_SYMBOLS
    + BOOM_CRASH_SYMBOLS + STEP_SYMBOLS + JUMP_BUILDUP_SYMBOLS
    + BEAR_BULL_SYMBOLS + DIGIT_SYMBOLS + DIGIT_PARITY_SYMBOLS
    + DRIFT_FADE_SYMBOLS
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
    # Boom/Crash — moderate (spike risk).
    # BOOM500/BOOM1000/CRASH500/CRASH1000: confirmed via the 2026-07-31
    # audit (real ranges x100-400 / x100-500), now in MULTIPLIER_SYMBOLS
    # above, values below (100) sit safely within range. buy_multiplier()
    # exists, so the method is no longer the blocker — but they still
    # won't actually trade until BOOM_CRASH_SYMBOLS is populated (still
    # empty, see BOOM_CRASH note up top) so they land in ALL_TRADE_SYMBOLS
    # and get routed to a strategy evaluator.
    # BOOM150/BOOM300/CRASH150/CRASH300 (un-suffixed): still UNAUDITED —
    # not in MULTIPLIER_SYMBOLS, values below are unverified guesses.
    "BOOM150": 100, "BOOM300": 100,
    "BOOM500": 100, "BOOM1000":100,
    "CRASH150":100, "CRASH300":100,
    "CRASH500":100, "CRASH1000":100,
    # Others
    "stpRNG":  200,
    # JD10-JD100: not yet in MULTIPLIER_SYMBOLS (Rise/Fall is used for
    # these right now, see RISE_FALL_SYMBOLS above; see the note by
    # MULTIPLIER_SYMBOLS for why they haven't been switched over).
    # Confirmed real ranges (2026-07-31 audit): JD10 x100-1000, JD25
    # x50-500, JD50 x20-200, JD75 x15-150, JD100 x10-100. JD50/JD75/JD100
    # below were capped down from 300/400/500 — those older values sat
    # outside the confirmed valid range and would have been rejected by
    # Deriv the moment Multiplier routing was ever turned on for them.
    "JD10":    100, "JD25":    200,
    "JD50":    200, "JD75":    150,
    "JD100":   100,
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
# For Multiplier contracts these remain the static outer boundary set at
# buy time — see ADAPTIVE EXIT ENGINE near the bottom of this file for
# the layer that trails stop_loss inside this boundary via
# contract_update, without changing this ratio itself.
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
# Real, actively-used constants — bot_engine.py reads these two names
# directly (previously it read CONTRACT_MAX_AGE_SECS/CONTRACT_FORCE_CLOSE_SECS
# which didn't exist here at all, silently falling back to unsafe 120s/300s
# hardcoded defaults against a real 14-minute/840s contract duration — see
# Implementation Brief v2, Fix B). Derived from TRADE_DURATION (14m = 840s)
# with a generous margin, per Deriv's documented multi-minute settlement lag.
CONTRACT_MAX_AGE_SECS     = 900     # trigger a non-destructive poll
CONTRACT_FORCE_CLOSE_SECS = 1350    # trigger active reconciliation (never a guess)

# Per-symbol Rise/Fall duration overrides (seconds omitted — same unit as
# TRADE_DURATION_UNIT). Populate here if a contracts_for audit finds a
# symbol that rejects the default TRADE_DURATION (14m). Empty = every
# symbol uses TRADE_DURATION/TRADE_DURATION_UNIT unchanged.
TRADE_DURATION_OVERRIDES = {}

# ── RECONCILIATION (never-fabricate-a-result path, Fix C) ────
# After CONTRACT_FORCE_CLOSE_SECS, a Rise/Fall contract that still hasn't
# settled moves to "reconcile_pending" instead of being marked a loss.
# It keeps polling on this cadence until it resolves for real, or until
# RECONCILE_MAX_SECS is hit, at which point it's escalated/logged loudly
# but STILL never assigned a guessed win/loss.
RECONCILE_POLL_INTERVAL_SECS = 30
RECONCILE_MAX_SECS           = 1800   # 30 min — far longer than any real
                                       # settlement should ever take

# ── MULTIPLIER CONTRACTS — explicit max-hold policy (Fix E) ──
# Multiplier contracts have no fixed expiry. If held this long, the bot
# actively calls sell_contract() to realize the real price (never a
# guess) and logs it as a deliberate time-based close — this replaces
# the old dead MAX_TRADE_OPEN_MINS/CHECK_TRADE_MINS constants, which were
# never actually read by anything.
# This remains the outer horizontal barrier either way — see the
# ADAPTIVE EXIT ENGINE section near the bottom of this file for the
# active management layer that now operates *inside* this bound
# (and inside STOP_LOSS_MAP / TAKE_PROFIT_RATIO below), rather than
# replacing it.
MULTIPLIER_MAX_HOLD_MINS = 30

# ── SYMBOL SUSPENSION (minutes) ──────────────────────────────
SYMBOL_WIN_SUSPEND_MINS   = 20     # unchanged — win path untouched
SYMBOL_MIN_GAP_MINS       = 1

# Escalating per-symbol loss suspension ladder (Implementation Brief v2,
# Requirement 2 / Fix F). Indexed by min(loss_count, len(ladder)) - 1, so
# 1st consecutive session loss on a symbol -> 60min, 2nd -> 120min,
# 3rd -> 180min, 4th and every further loss that session -> 240min.
# This counter/ladder is reset ONLY by a redeploy (a real process
# restart) — never by a UTC-midnight or other calendar boundary.
# Replaces the old flat SYMBOL_LOSS_SUSPEND_MINS / SYMBOL_SESSION_BAN_LOSSES
# (59,940-minute "session ban") scheme entirely.
SESSION_LOSS_SUSPEND_LADDER_MINS = [60, 120, 180, 240]

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
# fighting restart_scheduler.py's timer. Set high enough that it never fires
# on its own; restart_scheduler.py's daily Kenya-midnight timer (see
# REDEPLOY_TIMEZONE below) is the only authoritative redeploy trigger. Lower
# this back down only if you deliberately want a SECOND, settle-count-based
# redeploy path in addition to the daily timer.
REDEPLOY_EVERY_N_CYCLES = 999999
SETTLE_WAIT_SECS = 15

# restart_scheduler.py fires exactly once every 24h, at 00:00 in this zone
# (Africa/Nairobi = EAT = UTC+3 year-round, no DST) — replaces the old fixed
# 2-hour timer per Implementation Brief v2, Requirement 2 / Fix G.
REDEPLOY_TIMEZONE = "Africa/Nairobi"

# How long bot_engine.py's _settle_loop will wait, actively trying to
# confirm-close every remaining open contract, once a redeploy has been
# scheduled, before delaying the redeploy rather than wiping contract
# bookkeeping (Fix G). With daily (not 2-hourly) redeploys there's much
# more natural lead time, so this can be generous.
DRAIN_MAX_SECS = 1800

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

# Minimum logged trades a (strategy, symbol) pair needs before the Kelly
# overlay activates for it (see risk_manager.py compute_kelly_fraction()).
# Below this, the overlay is a no-op and PLS's stake passes through
# unchanged. Implementation Brief v5 / A3 — previously an invisible
# getattr(config, "KELLY_MIN_TRADES", 20) fallback inside risk_manager.py;
# now explicit and tunable here.
KELLY_MIN_TRADES = 20

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
# SUPERSEDED as of Implementation Brief v5 / B2 — bot_engine.py's
# _session_dow_weight() no longer reads this table. Its category keys
# never matched anything get_symbol_class() actually returns for this
# bot's traded symbols, so it was a dead no-op in practice. Left here
# for reference/rollback only; the live mechanism is now
# strategy_stats.stats.get_hourly_payout_ratio(), which weights on the
# bot's own realized win-payout ratio by UTC hour instead of a guess.
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
SESSION_DOW_WEIGHT_DEFAULT = 1.0  # applied when no table entry matches / not enough data

# ── HOURLY PAYOUT WEIGHTING (Implementation Brief v5 / B2) ────
# Minimum settled trades required in the CURRENT UTC hour's bucket, and
# across all hours combined, before _session_dow_weight() trusts the
# realized data enough to deviate from SESSION_DOW_WEIGHT_DEFAULT. Below
# these, behavior is a no-op (1.0) — this is deliberately conservative
# early on and only starts adjusting once real history backs it.
SESSION_HOURLY_MIN_TRADES = 15
SESSION_HOURLY_MIN_TOTAL_TRADES = 200
# Same [0.8, 1.2] bound the old static table used by convention.
SESSION_HOURLY_WEIGHT_MIN = 0.8
SESSION_HOURLY_WEIGHT_MAX = 1.2

# ══════════════════════════════════════════════════════════════
# ADAPTIVE EXIT ENGINE (Multiplier / non-time-bound contracts only)
# ══════════════════════════════════════════════════════════════
# Rise/Fall contracts are untouched by this — they keep using
# TRADE_DURATION/TRADE_DURATION_UNIT exactly as before (see SESSION /
# DAY-OF-WEEK section above; do not touch those two constants or
# TRADE_DURATION_OVERRIDES for this feature).
#
# Multipliers (MULTUP/MULTDOWN — MULTIPLIER_SYMBOLS above) have no fixed
# expiry; today they close only via the static STOP_LOSS_MAP /
# TAKE_PROFIT_RATIO set at buy time, or the blunt
# MULTIPLIER_MAX_HOLD_MINS forced close (see MULTIPLIER CONTRACTS
# section above). This engine actively manages the open contract
# between those two existing boundaries — trailing the stop-loss up as
# profit grows, and closing early if profit decays — instead of just
# waiting for one of the two static limits to fire. It never replaces
# STOP_LOSS_MAP, TAKE_PROFIT_RATIO, or MULTIPLIER_MAX_HOLD_MINS; those
# stay in force as the outer vertical/horizontal barriers this engine
# operates inside of. Lives in exit_engine.py (new file, built
# separately); revises stop_loss/take_profit on an already-open
# Multiplier contract via Deriv's contract_update request, wired up in
# deriv_client.py (also built separately). This section only adds the
# config surface it needs.
EXIT_ENGINE_ENABLED         = True
EXIT_ENGINE_SYMBOLS         = list(MULTIPLIER_SYMBOLS)  # only Multiplier contracts

# Rule-based trailing layer (always active — the ML layer below only ever
# adds an *earlier* close on top of this, never removes this safety net):
EXIT_ARM_PROFIT_FRACTION    = 0.30   # start trailing once profit >= 30% of the
                                      # contract's static take_profit_amount
EXIT_TRAIL_LOCK_FRACTION    = 0.60   # once armed, ratchet stop_loss to lock in
                                      # 60% of peak profit seen so far
EXIT_DECAY_CLOSE_FRACTION   = 0.25   # once armed, close immediately if profit
                                      # falls back below 25% of peak (rather than
                                      # waiting for the original static stop_loss)
EXIT_POLL_INTERVAL_SECS     = 15     # how often the exit engine re-checks each
                                      # open Multiplier contract (independent of
                                      # the general 30s orphan-sweep cadence)

# Lightweight ML layer (meta-labeling-inspired; reuses the existing
# META_LABEL_MIN_TRADES / META_LABEL_RETRAIN_EVERY_N constants defined
# above under META-LABELING — do not duplicate them here). Below
# META_LABEL_MIN_TRADES logged Multiplier-contract snapshots this is a
# strict no-op; only the rule-based layer above runs.
EXIT_ML_ENABLED             = True
EXIT_ML_MODEL_PATH          = "exit_model.joblib"  # ephemeral on Render free
                                                     # tier — resets on redeploy;
                                                     # acceptable for this
                                                     # research phase, retrains
                                                     # from fresh logs each time
EXIT_ML_FEATURE_WINDOW      = 5      # number of past polls used to compute
                                      # profit "velocity" as a feature
EXIT_ML_MIN_CONFIDENCE      = 0.60   # ML must be at least this confident a
                                      # reversal is coming to override the rule
                                      # layer's HOLD decision

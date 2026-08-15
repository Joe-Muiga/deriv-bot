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

# ── VOLATILITY / STEP FAMILY → MULTIPLIER MIGRATION ──────────
# Implementation Brief v4. Confirmed via symbol_audit.py, Render
# "symbol-audit" service, Aug 2026. Real contracts_for multiplier ranges —
# do not guess new values without re-running that audit; Deriv will reject
# anything outside [min, max].
#
# Moves these 11 symbols off Rise/Fall (CALL/PUT) and off the
# MEAN_REVERSION strategy entirely, onto Multiplier contracts
# (MULTUP/MULTDOWN) with the new VOL_BREAKOUT / VOL_REV_MULT strategy set
# (see signal_engine.py's evaluate_vol_regime() dispatcher). Boom/Crash
# (already on Multipliers) and Jump/Bear-Bull (deliberately staying on
# Rise/Fall — see notes above) are out of scope, untouched here.
VOL_MULTIPLIER_SYMBOLS = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    "stpRNG",
]

# Confirmed ranges (min, max) — for validation / future dynamic sizing.
VOL_MULTIPLIER_RANGES = {
    "R_10":    (400, 4000), "1HZ10V":  (400, 4000),
    "R_25":    (160, 1600), "1HZ25V":  (160, 1600),
    "R_50":    (80,  800),  "1HZ50V":  (80,  800),
    "R_75":    (50,  500),  "1HZ75V":  (50,  500),
    "R_100":   (40,  400),  "1HZ100V": (40,  400),
    "stpRNG":  (750, 7500),
}

# Phase 1 default = the confirmed FLOOR of each range (least leverage this
# account allows). Do not raise these without also validating the dynamic
# stop-loss sizing below handles the new multiplier correctly — the floor
# is the only value verified safe by hand in the brief.
for _sym, (_lo, _hi) in VOL_MULTIPLIER_RANGES.items():
    MULTIPLIER_MAP[_sym] = _lo

# Wire into the existing Multiplier routing gate — bot_engine._execute()
# already does `if symbol in config.MULTIPLIER_SYMBOLS: buy_multiplier(...)`.
# This is the ONLY change needed to stop these 11 symbols trading Rise/Fall.
# NOTE: must run before EXIT_ENGINE_SYMBOLS = list(MULTIPLIER_SYMBOLS)
# further down this file, so the Adaptive Exit Engine automatically picks
# up all 11 new symbols with zero additional wiring — it does, this block
# sits well before that line.
MULTIPLIER_SYMBOLS = list(dict.fromkeys(MULTIPLIER_SYMBOLS + VOL_MULTIPLIER_SYMBOLS))

# Retire Mean Reversion entirely — MEAN_REVERSION_SYMBOLS was exactly
# VOLATILITY_STANDARD + VOLATILITY_1S, i.e. these same 10 symbols (stpRNG
# was never in it). Emptying this list retires the strategy globally
# without deleting evaluate_mean_reversion() from signal_engine.py (left
# in place, unrouted, in case it's wanted again later).
MEAN_REVERSION_SYMBOLS = []

# Pull all 11 out of RISE_FALL_SYMBOLS and out of STEP_SYMBOLS (for
# stpRNG specifically) so they don't double-route — stpRNG now trades
# under VOL_MULTIPLIER_SYMBOLS's new evaluator instead of STEP's.
RISE_FALL_SYMBOLS = [s for s in RISE_FALL_SYMBOLS if s not in VOL_MULTIPLIER_SYMBOLS]
STEP_SYMBOLS = [s for s in STEP_SYMBOLS if s not in VOL_MULTIPLIER_SYMBOLS]

# ALL_TRADE_SYMBOLS is a derived union (see original definition above) —
# recomputed here now that RISE_FALL_SYMBOLS / MEAN_REVERSION_SYMBOLS /
# STEP_SYMBOLS have changed, and with VOL_MULTIPLIER_SYMBOLS added so
# these 11 symbols keep being scanned instead of dropping out entirely.
ALL_TRADE_SYMBOLS = list(dict.fromkeys(
    RISE_FALL_SYMBOLS + MEAN_REVERSION_SYMBOLS + RANGE_BREAK_SYMBOLS
    + BOOM_CRASH_SYMBOLS + STEP_SYMBOLS + JUMP_BUILDUP_SYMBOLS
    + BEAR_BULL_SYMBOLS + DIGIT_SYMBOLS + DIGIT_PARITY_SYMBOLS
    + DRIFT_FADE_SYMBOLS + VOL_MULTIPLIER_SYMBOLS
))
ALL_SYMBOLS        = ALL_TRADE_SYMBOLS
VOLATILITY_SYMBOLS = ALL_TRADE_SYMBOLS  # alias for compatibility with bot_engine.py

# ── DYNAMIC, ATR-NORMALIZED STOP-LOSS (Fix H) ─────────────────
# See §1 of the brief: STOP_LOSS_MAP's static percentages were calibrated
# for Boom/Crash's ~x100 multiplier. Copied unchanged onto e.g. R_10's
# x400 floor, a 30% stop_loss_pct works out to ~0.075% price movement —
# inside normal tick noise for a 2-second-tick index, so positions would
# get stopped out by noise, not by the market being wrong. Fix: compute
# stop_loss_pct from live ATR instead, so the dollar stop always
# corresponds to a stable number of ATRs of real price movement no
# matter which multiplier a symbol is forced into. STOP_LOSS_MAP /
# DEFAULT_STOP_LOSS_PCT stay untouched and keep governing Boom/Crash
# exactly as before — this only applies to VOL_MULTIPLIER_SYMBOLS (see
# RiskManager.compute_dynamic_stop_loss_pct() in risk_manager.py and its
# call site in bot_engine.py's _execute()).
DYNAMIC_STOP_LOSS_ENABLED   = True
STOP_ATR_MULT               = 2.0    # stop distance target, in ATRs of price
DYNAMIC_STOP_LOSS_PCT_MIN   = 15.0   # floor — never set a stop tighter than this
DYNAMIC_STOP_LOSS_PCT_MAX   = 90.0   # ceiling — leave headroom under Deriv's
                                      # own 100%-of-stake max-loss cap

# ── VOL REGIME DETECTION (for VOL_BREAKOUT / VOL_REV_MULT, signal_engine.py) ──
# ENHANCEMENT (win-rate pass, Aug 2026): dashboard trade history showed
# VOL_BREAKOUT losing on the large majority of its trades while carrying
# a healthy win/loss $ ratio — i.e. the direction/exit logic is fine, the
# entries firing on noise are the problem. Root cause: a single-bar
# ratio>=0.6 read let a transient EMA wiggle flip the regime to TREND for
# one cycle, routing straight into a breakout evaluator with no real
# trend behind it. Added a persistence requirement (VOL_REGIME_CONFIRM_BARS
# below) rather than changing which evaluator handles which regime — same
# two strategies, stricter gate on which one fires.
# CORRECTION (same pass, second iteration): the ratio itself was first
# raised 0.6 -> 0.85 alongside the persistence requirement — stacking both
# knobs at once turned out to suppress TREND classification almost
# entirely (dashboard went quiet across all 11 VOL_MULTIPLIER_SYMBOLS,
# leaving only BOOM_CRASH visible). 0.6 was never really the problem —
# no persistence requirement was. Reverted the ratio to its original
# value and kept persistence as the only added lever.
VOL_REGIME_TREND_RATIO = 0.6   # |EMA_fast-EMA_slow| / ATR >= this -> TREND,
                                # else RANGE. Back to its original value —
                                # see CORRECTION note above.
VOL_REGIME_CONFIRM_BARS = 2    # the ratio must clear VOL_REGIME_TREND_RATIO
                                # on this many consecutive completed bars
                                # (not just the latest) before the regime
                                # is called TREND. Any NaN/insufficient
                                # history in the window defaults to RANGE
                                # (the more conservative evaluator).

# ── VOL_BREAKOUT ENTRY CONFIRMATION (win-rate pass, Aug 2026) ─────────────
# A close that merely touches the Donchian channel edge was being scored
# as a full breakout — on 2s/1m synthetic ticks that's frequently just
# noise. BREAKOUT_MARGIN_ATR requires the close to clear the channel by a
# real distance (in ATRs) before it counts. Same Donchian+EMA+MACD scoring
# model as before — this only tightens what counts as "broke the level".
# CORRECTION (same pass, second iteration): an earlier version of this
# fix also required the *prior* bar to already be sitting near the
# channel edge, on the theory that would filter single-tick spikes.
# In practice that blocks the sharp, decisive candle a real breakout
# often is — it only let through slow grinding moves that were already
# extended, which is backwards for an entry strategy. Removed; the ATR
# margin alone is the filter now.
# CORRECTION (win-rate pass, Aug 2026, third iteration): live Render logs
# showed VOL_BREAKOUT consistently landing at 4/7 (EMA+MACD agree, break
# condition alone fails) across many different VOL_MULTIPLIER_SYMBOLS,
# never reaching the 6/7 fire threshold — for 8+ days straight, zero
# VOL_BREAKOUT trades. A fresh 20-bar Donchian high on a near-random-walk
# instrument typically only clears the prior high by a small fraction of
# ATR, not 15% of it — 0.15 was still too strict even after already being
# implicated once in this same pass. Lowered to 0.05: still requires a
# real move past the level (not a bare 1-tick touch, the original bug),
# just not an unrealistically large one.
BREAKOUT_MARGIN_ATR = 0.05

# ── VOL_REV_MULT ENTRY CONFIRMATION (win-rate pass, Aug 2026) ────────────
# When True, evaluate_vol_reversion_mult() requires the latest close to
# have already ticked back toward the mean vs. the prior close (not just
# RSI/BB/ROC sitting at an extreme) before firing — cuts entries taken
# while price is still accelerating into the extreme ("catching a falling
# knife"). Does not change the RSI/Bollinger/ROC thresholds that define
# the setup itself.
MEAN_REV_REQUIRE_TURN = True

# Take profit = stop loss × this ratio.
# ENHANCEMENT (win-rate pass, Aug 2026): raised 2.0 -> 2.5. Dashboard
# history already shows winners running several multiples larger than
# losers ($ magnitude) — this gives the exit engine's trailing layer
# (below) more room to ride a genuine winner before the static outer
# boundary force-closes it, without touching the stop-loss side (and
# therefore without changing per-trade downside risk).
# For Multiplier contracts these remain the static outer boundary set at
# buy time — see ADAPTIVE EXIT ENGINE near the bottom of this file for
# the layer that trails stop_loss inside this boundary via
# contract_update, without changing this ratio itself.
TAKE_PROFIT_RATIO = 2.5

# ── STAKE SETTINGS ───────────────────────────────────────────
BASE_STAKE_PCT       = 0.005   # 0.5% of current balance per trade — this
                                # IS the compounding: stake grows/shrinks
                                # automatically as balance grows/shrinks.
MIN_STAKE            = 5      # safety floor only. FIX (profitability audit):
                                # was 100, which is ABOVE what 0.5% of a
                                # typical account (~$8-9k -> $40-45) works
                                # out to. That meant every single trade was
                                # silently forced to the $100 floor instead
                                # of the balance-based/Kelly-adjusted stake —
                                # dashboard history confirms every logged
                                # trade was exactly $100.00 regardless of
                                # signal strength or edge. Set safely below
                                # BASE_STAKE_PCT × balance for realistic
                                # account sizes so PLS/Kelly sizing actually
                                # drives stake again; raise only if you want
                                # a higher effective per-trade minimum.
MAX_STAKE            = 1000.0  # safety backstop only, not the everyday driver.
DAILY_LOSS_LIMIT_PCT = 0.06    # FIX: was 0.15 (15%) — too loose to act as a
                                # real circuit breaker. 6% is a more typical
                                # prudent daily stop for leveraged multiplier
                                # trading; tune to taste but keep well under 15%.
DAILY_LOSS_PAUSE_MINS = 30

# FIX (profitability audit, round 2): global, account-wide circuit breaker —
# pause ALL new entries (any symbol/strategy) after this many consecutive
# losses, independent of the %-based DAILY_LOSS_LIMIT_PCT above. Added
# because a bad run (e.g. 5 losses in a 7-trade session) previously had
# nothing account-wide stopping it short of that much coarser daily-%
# threshold. See BotEngine._global_consecutive_losses.
GLOBAL_CONSECUTIVE_LOSS_LIMIT = 4
GLOBAL_CONSECUTIVE_LOSS_PAUSE_MINS = 45

# ── EQUITY CURVE STABILIZATION (win-rate/drawdown pass, Aug 2026) ─────────
# The circuit breaker above is binary: trading stops entirely for
# GLOBAL_CONSECUTIVE_LOSS_PAUSE_MINS once it trips, then resumes at full
# size. Between "nothing" and "fully paused" there was no way for stake
# to ease down smoothly during a rough patch and ease back up as it
# recovers — every red dot on the balance curve landed at the same $
# size as every green one, which is what makes the curve zig-zag.
# RiskManager._stability_dampener_mult() (risk_manager.py) applies a
# continuous multiplier on top of PLS/Kelly, driven by two signals, and
# takes whichever is more conservative rather than multiplying them
# (they're correlated — a loss streak IS a drawdown — so multiplying
# would double-punish the same event):
#   1. Distance below the balance high-water mark (drawdown %)
#   2. Consecutive losses since the last win
# Both recover automatically as balance/streak improve — no separate
# "unpause" event needed, unlike the hard breaker above.
DRAWDOWN_DAMPENER_ENABLED  = True
DRAWDOWN_DAMPENER_START_PCT = 0.015  # below this drawdown from peak balance,
                                       # no throttling at all (1.0x)
DRAWDOWN_DAMPENER_FULL_PCT  = 0.06   # drawdown at which the floor multiplier
                                       # is reached — matches DAILY_LOSS_LIMIT_PCT
                                       # so the dampener has fully engaged by the
                                       # time the hard daily-loss breaker would trip
DRAWDOWN_DAMPENER_FLOOR     = 0.40   # stake never shrinks below 40% of normal
                                       # from this signal alone

LOSS_STREAK_DAMPENER_ENABLED = True
# (consecutive losses since last win) -> stake multiplier. First entry is
# the implicit 0-1 loss baseline (no throttle); each tuple after that is
# (streak_count, multiplier), checked in order, last match wins.
LOSS_STREAK_DAMPENER_TABLE = [
    (2, 0.85),
    (3, 0.70),
    (4, 0.55),  # GLOBAL_CONSECUTIVE_LOSS_LIMIT=4 hard-pauses right after
                # this tier — the floor here is deliberately close to
                # LOSS_STREAK_DAMPENER's own floor rather than needing a
                # tier of its own beyond this point.
]

# ── AGGRESSIVE COMPOUNDING ───────────────────────────────────
# Disabled per user request — stake no longer scales up on win streaks.
# Multipliers all set to 1.0 so PLS tier lookups (wherever risk_manager.py
# applies them) are a no-op; stake stays flat regardless of streak length.
PLS_WIN_THRESHOLDS  = [3,   5,   8,   12,  15  ]
PLS_WIN_MULTIPLIERS = [1.0, 1.0, 1.0, 1.0, 1.0]
PLS_WIN_EXTRA_SLOTS = [0,   0,   0,   0,   0   ]

# ── CONCURRENT TRADES ────────────────────────────────────────
# FIX (profitability audit): was 30. With every trade effectively forced to
# $100 (see MIN_STAKE fix above) that allowed up to $3,000 of simultaneous
# exposure — a large fraction of account equity open at once, much of it in
# highly-correlated symbols (e.g. R_10 and 1HZ10V both track the same
# volatility parameter). Lowered to reduce simultaneous drawdown risk;
# raise gradually only once live win-rate/profit-factor justify it.
MAX_CONCURRENT_TRADES = 6

# Correlated-symbol grouping — synthetic indices sharing the same underlying
# volatility parameter (just different tick generation) move together far
# more than unrelated symbols do, so treating them as independent slots
# understates real concurrent risk. Caps how many concurrently-open
# positions may share a family, on top of MAX_CONCURRENT_TRADES overall.
# See bot_engine.py's execution loop for the enforcement point.
SYMBOL_FAMILY_MAP = {
    "R_10": "VOL10", "1HZ10V": "VOL10",
    "R_25": "VOL25", "1HZ25V": "VOL25",
    "R_50": "VOL50", "1HZ50V": "VOL50",
    "R_75": "VOL75", "1HZ75V": "VOL75",
    "R_100": "VOL100", "1HZ100V": "VOL100",
    "BOOM500": "BOOMCRASH500", "CRASH500": "BOOMCRASH500",
    "BOOM1000": "BOOMCRASH1000", "CRASH1000": "BOOMCRASH1000",
}
MAX_CONCURRENT_PER_FAMILY = 2

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

# restart_scheduler.py fires every REDEPLOY_INTERVAL_HOURS, anchored to
# 00:00 in this zone (Africa/Nairobi = EAT = UTC+3 year-round, no DST) —
# so with the default of 6 that's 00:00 / 06:00 / 12:00 / 18:00 EAT (4
# redeploys/day). Was a once-daily fixed 00:00 timer per Implementation
# Brief v2, Fix G; widened to 4x/day on request — see restart_scheduler.py's
# _next_scheduled_fire().
REDEPLOY_TIMEZONE = "Africa/Nairobi"
REDEPLOY_INTERVAL_HOURS = 6

# How long bot_engine.py's _settle_loop will wait, actively trying to
# confirm-close every remaining open contract, once a redeploy has been
# scheduled, before delaying the redeploy rather than wiping contract
# bookkeeping (Fix G). Kept generous even at 4x/day — 30min of drain
# headroom out of every 6h window is still cheap, and a redeploy that's
# delayed a few minutes because a contract is still confirming its close
# is far better than one that guesses.
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
TRADE_DURATION = 6
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
# Once a (strategy, symbol) pair has this many logged trades, flag it as
# underperforming if its win rate falls below the floor. FIX (profitability
# audit): was 100 — is_underperforming() sat completely unused by any
# execution path (dead code, confirmed by grep across the codebase), so
# raising this wasn't even the bottleneck; the real fix is wiring it into
# signal_engine.SignalEngine.evaluate() (done) with a threshold low enough
# to matter before large losses accumulate. 30 aligns with KELLY_MIN_TRADES's
# order of magnitude below.
STRATEGY_WIN_RATE_FLOOR = 0.55
STRATEGY_WIN_RATE_MIN_TRADES = 30

# ── META-LABELING (future ML filter) ──────────────────────────
META_LABEL_MIN_TRADES      = 200   # trades required before the filter is trusted
META_LABEL_RETRAIN_EVERY_N = 100   # retrain cadence, in newly logged trades
# FIX (profitability audit): required buffer above breakeven for the EV
# gate in meta_labeling.predict_take_trade() — was an implicit 0.0, taking
# any trade with a nominally-positive point estimate regardless of how
# noisy that estimate was.
META_LABEL_EV_MARGIN        = 0.03

# ── SIGNAL DIRECTION INVERSION (win-rate/drawdown pass, Aug 2026) ────────
# meta_labeling.predict_take_trade() can return "INVERT" in addition to
# "TAKE"/"SKIP": once a (strategy, symbol) pair's per-pair EV model has
# enough history (META_LABEL_EV_MIN_FEATURE_ROWS rows, config below) to
# produce a real estimate of p(win | features) for the signal's ORIGINAL
# direction, and that estimate is low enough that the OPPOSITE direction
# has the better expected value, bot_engine._execute() flips sig.direction
# before placing the order — same entry price, same ATR-based stop/target
# sizing (already symmetric for LONG vs SHORT), just the side is flipped.
# This cannot fire for a pair until it has real evidence — below
# META_LABEL_EV_MIN_FEATURE_ROWS this always returns TAKE, never a guess.
# Simplifying assumption: the inverted direction's win probability is
# approximated as (1 - p_hat_original) and its payout ratio as the same
# avg_ratio already measured for the original direction. Real
# execution/spread asymmetries mean this is an approximation, not exact —
# worth revisiting once enough inverted trades exist to measure their own
# realized payout ratio directly instead of borrowing the original's.
META_LABEL_INVERT_ENABLED = True
INVERT_MIN_CONFIDENCE     = 0.65  # only invert when the OPPOSITE direction's
                                    # estimated win probability clears this —
                                    # deliberately higher than the plain
                                    # META_LABEL_EV_MARGIN skip bar, since
                                    # inverting is a stronger claim than
                                    # simply not trusting the original signal

# NOTE: an earlier iteration of this design had a separate
# META_LABEL_NO_SKIP_STRATEGIES set (BOOM_CRASH only) alongside a
# strategy-level default-action map. Superseded by the per-symbol default
# map below — no symbol is ever skipped outright now (the gate always
# picks between a symbol's default and its opposite), so a standalone
# no-skip list is redundant with that design and has been removed.

# ── PER-SYMBOL DEFAULT DIRECTION (win-rate/drawdown pass, Aug 2026) ──────
# User-directed design, refined from a strategy-level default to a
# symbol-level one: the "H..." volatility symbols (1HZ10V/25V/50V/75V/100V)
# default to TAKE (their own coded strategy, un-inverted); BOOM/CRASH
# symbols default to INVERT; the remaining VOL_MULTIPLIER_SYMBOLS — the
# plain R_xx symbols and stpRNG — also default to INVERT. AI/ML only
# steers a symbol off its default once the per-pair EV model has
# accumulated enough evidence (META_LABEL_EV_MIN_FEATURE_ROWS rows) that
# doing so is actually the better bet.
#
# IMPORTANT HONESTY NOTE, read before changing these defaults: below the
# per-pair data threshold there is no real evidence either way for that
# SPECIFIC symbol — every one of these starting postures is a directional
# choice the user made deliberately, not something any model calculated.
# Once a symbol crosses the data threshold, its action becomes genuinely
# evidence-based and can move off its default.
#
# Built from the symbol lists above rather than hand-typed, so this can
# never silently drift out of sync with VOL_MULTIPLIER_SYMBOLS/BOOM_CRASH
# if either list changes later.
META_LABEL_DEFAULT_ACTION_BY_SYMBOL: dict = {
    **{s: "TAKE" for s in VOL_MULTIPLIER_SYMBOLS if s.startswith("1HZ")},
    **{s: "INVERT" for s in BOOM_CRASH},
    **{s: "INVERT" for s in VOL_MULTIPLIER_SYMBOLS if not s.startswith("1HZ")},
}
# Fallback for any symbol not explicitly listed above (e.g. JD10-JD100,
# RDBEAR/RDBULL — strategies where INVERT isn't even applicable, see
# bot_engine._execute()'s contract_kind guard) — TAKE, i.e. behave as
# though this whole feature didn't exist for them.
META_LABEL_DEFAULT_ACTION_FALLBACK = "TAKE"
TAKE_MIN_CONFIDENCE = 0.65  # mirror of INVERT_MIN_CONFIDENCE below, for
                             # symbols whose default is INVERT: only
                             # override back to the ORIGINAL direction once
                             # its estimated win probability clears this


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
# ENHANCEMENT (win-rate pass, Aug 2026): retuned all three fractions.
# 30%-to-arm / 60%-lock / 25%-decay was letting a lot of paper profit
# round-trip back into a loss (or a much smaller win) on a low-win-rate
# strategy before the trailing layer ever engaged. Arming earlier and
# locking a bigger share of peak profit banks more of every winner —
# raises effective win/loss $ skew without touching entry logic.
EXIT_ARM_PROFIT_FRACTION    = 0.15   # start trailing once profit >= 15% of the
                                      # contract's static take_profit_amount
                                      # (was 0.30 — armed too late)
EXIT_TRAIL_LOCK_FRACTION    = 0.75   # once armed, ratchet stop_loss to lock in
                                      # 75% of peak profit seen so far
                                      # (was 0.60 — gave back too much)
EXIT_DECAY_CLOSE_FRACTION   = 0.20   # once armed, close immediately if profit
                                      # falls back below 20% of peak (rather than
                                      # waiting for the original static stop_loss)
                                      # (was 0.25)
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

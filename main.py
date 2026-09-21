"""
main.py – Entry point for the SIFM Deriv Trading Bot on Render.

Architecture:
  • Thread 1 (main) : Flask web server for Render's health checks —
                       always running, regardless of strategy-cycle phase.
  • Thread 2        : asyncio event loop running the bot engine — only
                       started while strategy_cycle.py's cycle is in its
                       TRADING phase (see below).
  • Thread 3        : self-ping keep-alive (pings /health every 40 s) —
                       always running, so Render sees a healthy service
                       24/7 even while the bot is deliberately not
                       connected to Deriv during a strategy-switch
                       cooldown.
  • Thread 4        : restart scheduler (rolling redeploy every
                       config.REDEPLOY_INTERVAL_HOURS) — only started in
                       the TRADING phase.
  • Thread 5        : strategy-cycle cooldown supervisor — only started
                       while the cycle is in its COOLDOWN phase, or the
                       instant TRADING transitions into COOLDOWN
                       (started from inside strategy_cycle.py itself in
                       that case, not from here).

Donkey RAW/ORIGINAL balance-trend switch (Sep 2026, see
strategy_cycle.py): only one Donkey variant trades at a time, for
roughly config.STRATEGY_SWITCH_MIN_MINUTES to STRATEGY_SWITCH_MAX_MINUTES
(an hour to 3 hours). The switch is triggered by a balance-trend
reversal — rising for a while, then suddenly falling — not a fixed
timer, OR by the account balance falling
config.STRATEGY_SWITCH_STARTING_DRAWDOWN_PCT below the run's starting
balance at the moment the ordinary rolling redeploy comes due (see
strategy_cycle.is_starting_drawdown_breached()). When either happens
bot_engine drains open contracts, disconnects from Deriv, and hands off
to a 1-hour (config.STRATEGY_SWITCH_COOLDOWN_MINUTES) cooldown during
which this process is NOT connected to Deriv at all — only health
checks. When the cooldown elapses, a Render redeploy is fired
automatically, and the fresh deploy resumes trading on the other
variant. This repeats forever.

Profit-target cycle (Sep 2026, see profit_cycle.py): runs independently
of, and alongside, the Donkey switch above. Trades until the balance is
config.PROFIT_CYCLE_TARGET_PCT above this cycle's own starting balance
— reaching that target immediately blocks any further new trade from
firing, at any point while the bot is live — or until the balance falls
config.PROFIT_CYCLE_STARTING_DRAWDOWN_PCT below that same starting
balance at the moment the ordinary rolling redeploy comes due. Either
way: drain open contracts, disconnect, and cooldown for
config.PROFIT_CYCLE_COOLDOWN_MINUTES before redeploying into a
brand-new cycle. Forever.

main.py's only job for either feature is deciding, once per boot,
whether this deploy should start the bot at all or just sit in
cooldown — everything else lives in strategy_cycle.py / profit_cycle.py,
signal_engine.py and bot_engine.py. The two cycles are independent and
either can be mid-cooldown at boot without the other being; this deploy
sits out (bot thread + rolling scheduler not started) if EITHER one is.

Environment variables required:
  DERIV_API_TOKEN        – your Deriv API token (trade + read scope)
  DERIV_APP_ID           – your Deriv app ID  (default 1089 = demo)
  RENDER_EXTERNAL_URL    – set automatically by Render  (e.g. https://yourapp.onrender.com)
  PORT                   – set automatically by Render   (default 8080)
  RENDER_DEPLOY_HOOK_URL – deploy hook URL from Render dashboard (required
                            for either cycle's cooldown to actually
                            redeploy once it elapses)
  RENDER_API_KEY         – Render API key (required for the active
                            variant / balance-trend state, and the
                            profit-cycle's own state, to survive a
                            redeploy — see strategy_cycle.py /
                            profit_cycle.py)
  RENDER_SERVICE_ID      – this service's srv-xxxxxxxx id (same as above)
"""

import asyncio
import logging
import threading
import sys
import time
import config
import strategy_cycle
import profit_cycle

# ─── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = getattr(logging, config.LOG_LEVEL, logging.INFO),
    format  = "%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    stream  = sys.stdout,
)
logger = logging.getLogger("main")

# ─── Sanity check ─────────────────────────────────────────────────────────────
if not config.DERIV_API_TOKEN:
    logger.critical(
        "DERIV_API_TOKEN is not set!\n"
        "Add it as an environment variable in your Render service settings.\n"
        "Get your token at: https://app.deriv.com/account/api-token"
    )
    # Don't exit – let the web server start so Render won't kill the service.

# ─── Bot thread ───────────────────────────────────────────────────────────────
def _run_bot():
    from bot_engine import BotEngine
    bot = BotEngine()
    try:
        asyncio.run(bot.run())
    except Exception as exc:
        logger.error(f"Bot crashed: {exc}", exc_info=True)
    else:
        # run() can now return normally (not just via exception/cancel)
        # when bot_engine decided to enter a strategy-switch cooldown or
        # a profit-cycle cooldown — see bot_engine.py's _main_loop /
        # _settle_loop. Nothing further to do here: whichever cycle's
        # enter_cooldown_now() ran already persisted its own cooldown
        # window and started its own supervisor thread before _main_loop
        # stopped itself, so this thread simply ends.
        logger.info(
            "Bot engine thread exited (redeploy-pending, strategy-"
            "cooldown-entered, or profit-cooldown-entered)")

# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from keep_alive import app, start_keep_alive
    from restart_scheduler import start_restart_scheduler

    logger.info("Starting SIFM Deriv Trading Bot …")
    logger.info(f"  Deriv App ID : {config.DERIV_APP_ID}")
    logger.info(f"  API token    : {'SET ✓' if config.DERIV_API_TOKEN else 'MISSING ✗'}")
    logger.info(f"  Port         : {config.PORT}")
    logger.info(f"  Self-URL     : {config.SELF_URL}")

    # 1. Start keep-alive pinger — ALWAYS, in every phase. This (plus the
    #    Flask app started at the bottom) is what keeps Render's health
    #    checks green during a strategy-switch cooldown even though the
    #    bot is deliberately not connected to Deriv.
    start_keep_alive()

    # 2. Cooldown phase check (Sep 2026) — decides whether this deploy
    #    should start the bot at all or just sit in cooldown. Two
    #    independent cycles, either of which can be mid-cooldown without
    #    the other being: the Donkey ORIGINAL/RAW balance-trend switch
    #    (strategy_cycle.py) and the profit-target cycle
    #    (profit_cycle.py). This deploy sits out — bot thread and rolling
    #    restart scheduler both stay off — if EITHER cooldown is active;
    #    each cooldown's own supervisor thread (started below) fires the
    #    Render deploy hook independently once its own window elapses.
    strat_cooldown_until   = strategy_cycle.in_cooldown_at_boot()
    profit_cooldown_until  = profit_cycle.in_cooldown_at_boot()

    if strat_cooldown_until is not None or profit_cooldown_until is not None:
        if strat_cooldown_until is not None:
            remaining_min = max(0.0, strat_cooldown_until - time.time()) / 60
            logger.warning(
                "\n" + "=" * 64 + "\n"
                f"STRATEGY-CYCLE COOLDOWN ACTIVE — {remaining_min:.1f} min "
                "remaining.\n"
                "This deploy will NOT connect to Deriv or start the bot "
                "engine — health checks only until the cooldown supervisor "
                "fires the next redeploy onto the new variant.\n" + "=" * 64
            )
            strategy_cycle.start_cooldown_supervisor(strat_cooldown_until)

        if profit_cooldown_until is not None:
            remaining_min = max(0.0, profit_cooldown_until - time.time()) / 60
            logger.warning(
                "\n" + "=" * 64 + "\n"
                f"PROFIT-CYCLE COOLDOWN ACTIVE — {remaining_min:.1f} min "
                "remaining.\n"
                "This deploy will NOT connect to Deriv or start the bot "
                "engine — health checks only until the cooldown supervisor "
                "fires the next redeploy into a brand-new cycle.\n" + "=" * 64
            )
            profit_cycle.start_cooldown_supervisor(profit_cooldown_until)

        # Neither the bot thread nor the rolling restart scheduler are
        # started in this branch — there's nothing for either to do
        # while intentionally disconnected from Deriv, and starting the
        # restart scheduler here would just spam "redeploy pending"
        # warnings forever since nothing would ever drain/confirm it.

    else:
        # Either genuinely trading already, or a cooldown that has
        # already elapsed by the time this deploy came up for either
        # cycle (i.e. this deploy IS the resumption a cooldown supervisor
        # triggered) — *_cooldown_until being None covers both, for each
        # cycle independently.
        if strategy_cycle.load_state()["phase"] == "cooldown":
            # The second case above, for the strategy cycle — flip the
            # persisted phase back to "trading" now so it doesn't linger
            # as a stale "cooldown" forever. strategy_cycle.
            # enter_cooldown_now() already swapped STRAT_ACTIVE_VARIANT
            # and cleared the run-tracking fields when the cooldown
            # began, so bot_engine.run() -> get_or_init_phase() below
            # will capture a fresh run on the new variant regardless —
            # this call is just for a clean, non-stale phase record.
            strategy_cycle.clear_cooldown_and_resume_trading()

        if profit_cycle.load_state()["phase"] == "cooldown":
            # Same idea, for the profit cycle — profit_cycle.
            # enter_cooldown_now() already cleared CYCLE_STARTING_BALANCE
            # when the cooldown began, so bot_engine.run() ->
            # get_or_init_starting_balance() below will capture a fresh
            # starting balance regardless; this is just for a clean,
            # non-stale phase record.
            profit_cycle.clear_cooldown_and_start_new_cycle()

        # 3. Start auto-redeploy scheduler (no-op if RENDER_DEPLOY_HOOK_URL
        #    is unset) — rolling redeploy every REDEPLOY_INTERVAL_HOURS
        #    while the bot is ON/trading.
        start_restart_scheduler()

        # 4. Start bot in background thread
        bot_thread = threading.Thread(target=_run_bot, name="bot-engine", daemon=True)
        bot_thread.start()
        logger.info("Bot engine thread started ✓")

    # 5. Flask in main thread (Render requires a bound web server) —
    #    ALWAYS, in every phase.
    logger.info(f"Starting Flask on 0.0.0.0:{config.PORT}")
    app.run(host="0.0.0.0", port=config.PORT, use_reloader=False, threaded=True)

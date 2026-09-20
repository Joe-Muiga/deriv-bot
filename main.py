"""
main.py – Entry point for the SIFM Deriv Trading Bot on Render.

Architecture:
  • Thread 1 (main) : Flask web server for Render's health checks —
                       always running, in every phase.
  • Thread 2        : asyncio event loop running the bot engine —
                       only started while the profit-target cycle is in
                       its TRADING phase (see profit_cycle.py).
  • Thread 3        : self-ping keep-alive (pings /health every 40 s) —
                       always running, in every phase, so Render sees a
                       healthy service 24/7 even while the bot is
                       deliberately not connected to Deriv.
  • Thread 4        : restart scheduler (rolling redeploy every
                       config.REDEPLOY_INTERVAL_HOURS, default 5 min) —
                       only started in the TRADING phase.
  • Thread 5        : profit-cycle cooldown supervisor — only started
                       while the profit-target cycle is in its COOLDOWN
                       phase, or the instant TRADING transitions into
                       COOLDOWN (started from inside profit_cycle.py
                       itself in that case, not from here).

Profit-target cycle (Sep 2026, see profit_cycle.py):
  The bot trades from a persisted "cycle starting balance" until the
  live balance has grown config.PROFIT_CYCLE_TARGET_PCT (default 50%)
  above it. At that point bot_engine drains open contracts, disconnects
  from Deriv, and hands off to a 17-minute (config.
  PROFIT_CYCLE_COOLDOWN_MINUTES) cooldown during which this process is
  NOT connected to Deriv at all — only health checks. When the cooldown
  elapses, a Render redeploy is fired automatically, and the fresh
  deploy starts a brand-new cycle. This repeats forever. main.py's only
  job for this feature is deciding, once per boot, whether this deploy
  should start the bot at all or just sit in cooldown — everything else
  lives in profit_cycle.py and bot_engine.py.

Environment variables required:
  DERIV_API_TOKEN        – your Deriv API token (trade + read scope)
  DERIV_APP_ID           – your Deriv app ID  (default 1089 = demo)
  RENDER_EXTERNAL_URL    – set automatically by Render  (e.g. https://yourapp.onrender.com)
  PORT                   – set automatically by Render   (default 8080)
  RENDER_DEPLOY_HOOK_URL – deploy hook URL from Render dashboard (required
                            for both the rolling redeploy AND the
                            profit-cycle cooldown to actually redeploy)
  RENDER_API_KEY         – Render API key (required for the profit-cycle's
                            starting balance / phase / cooldown deadline
                            to survive a redeploy — see profit_cycle.py)
  RENDER_SERVICE_ID      – this service's srv-xxxxxxxx id (same as above)
"""

import asyncio
import logging
import threading
import sys
import time
import config
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
        # when bot_engine decided to enter a profit-cycle cooldown — see
        # bot_engine.py's _main_loop / _settle_loop. Nothing further to
        # do here: profit_cycle.enter_cooldown_now() already persisted
        # the cooldown window and started its own supervisor thread
        # before _main_loop stopped itself, so this thread simply ends.
        logger.info("Bot engine thread exited (redeploy-pending or cooldown-entered)")

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
    #    checks green during a profit-cycle cooldown even though the bot
    #    is deliberately not connected to Deriv.
    start_keep_alive()

    # 2. Profit-target cycle phase check (Sep 2026) — decides whether
    #    this deploy should start the bot at all. See profit_cycle.py.
    cooldown_until = profit_cycle.in_cooldown_at_boot()

    if cooldown_until is not None:
        remaining_min = max(0.0, cooldown_until - time.time()) / 60
        logger.warning(
            "\n" + "=" * 64 + "\n"
            f"PROFIT-CYCLE COOLDOWN ACTIVE — {remaining_min:.1f} min "
            "remaining.\n"
            "This deploy will NOT connect to Deriv or start the bot "
            "engine — health checks only until the cooldown supervisor "
            "fires the next redeploy.\n" + "=" * 64
        )
        # Neither the bot thread nor the rolling restart scheduler are
        # started in this branch — there's nothing for either to do
        # while intentionally disconnected from Deriv, and starting the
        # restart scheduler here would just spam "redeploy pending"
        # warnings forever since nothing would ever drain/confirm it.
        profit_cycle.start_cooldown_supervisor(cooldown_until)

    else:
        # Either genuinely trading already, or a cooldown that has
        # already elapsed by the time this deploy came up (i.e. this
        # deploy IS the resumption the cooldown supervisor triggered) —
        # profit_cycle.in_cooldown_at_boot() returning None covers both.
        if profit_cycle.load_state()["phase"] == "cooldown":
            # The second case above — flip the persisted phase back to
            # "trading" now so it doesn't linger as a stale "cooldown"
            # forever. profit_cycle.enter_cooldown_now() already cleared
            # CYCLE_STARTING_BALANCE when the cooldown began, so
            # bot_engine.run() -> get_or_init_starting_balance() below
            # will capture a brand-new cycle starting balance regardless
            # — this call is just for a clean, non-stale phase record.
            profit_cycle.clear_cooldown_and_start_new_cycle()

        # 3. Start auto-redeploy scheduler (no-op if RENDER_DEPLOY_HOOK_URL
        #    is unset) — rolling redeploy every REDEPLOY_INTERVAL_HOURS
        #    (5 min by default) while the bot is ON/trading.
        start_restart_scheduler()

        # 4. Start bot in background thread
        bot_thread = threading.Thread(target=_run_bot, name="bot-engine", daemon=True)
        bot_thread.start()
        logger.info("Bot engine thread started ✓")

    # 5. Flask in main thread (Render requires a bound web server) —
    #    ALWAYS, in every phase.
    logger.info(f"Starting Flask on 0.0.0.0:{config.PORT}")
    app.run(host="0.0.0.0", port=config.PORT, use_reloader=False, threaded=True)

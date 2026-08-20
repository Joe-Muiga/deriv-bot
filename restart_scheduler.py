"""
restart_scheduler.py – rolling redeploy scheduler.

Spec point 11 (Aug 2026): the bot force-redeploys itself every
config.REDEPLOY_INTERVAL_HOURS hours, measured from the last actual
redeploy/resumption — NOT anchored to a fixed clock time. Replaces the
previous fixed-Africa/Nairobi-midnight schedule (_next_nairobi_midnight()),
which ignored config.REDEPLOY_INTERVAL_HOURS entirely and always fired once
a day regardless of what that config value was set to.

Public interface (unchanged contract with bot_engine.py):
  - is_redeploy_pending() -> bool
        True once the rolling timer has fired and a redeploy is due.
        bot_engine.py's _main_loop() checks this to pause taking on new
        trades, and _settle_loop() checks it to know when to start
        draining open contracts before actually redeploying.
  - trigger_redeploy() -> None
        Called by bot_engine.py's _settle_loop() ONLY once every open
        contract has been actively, confirmably closed (or the drain
        window has been exceeded and the redeploy is being delayed —
        in which case this is NOT called, see bot_engine.py Fix G).
        Actually fires the Render deploy hook and clears the pending flag.
  - run_scheduler() -> coroutine
        The rolling timer loop itself. Started as a background task by
        bot_engine.py's run() alongside its other loops.

Draining coordination lives entirely in bot_engine.py (it owns the open
contracts and the reconciliation / Multiplier max-hold machinery) — this
module only owns the schedule and the actual deploy-hook trigger.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

import config

logger = logging.getLogger(__name__)

# ── Module-level scheduler state ────────────────────────────────────────────
_pending: bool = False
_last_triggered_at: float = 0.0
_last_scheduled_at: float = 0.0


def is_redeploy_pending() -> bool:
    """True once the rolling REDEPLOY_INTERVAL_HOURS timer has fired and a
    redeploy is due but hasn't been confirmed-triggered yet."""
    return _pending


async def run_scheduler():
    """
    Sleeps for config.REDEPLOY_INTERVAL_HOURS, measured from the moment
    this loop iteration starts (i.e. from the last actual redeploy or from
    process start on the very first iteration) — not anchored to any fixed
    wall-clock time. Sets the pending flag, then repeats.
    bot_engine.py's _settle_loop() is responsible for noticing
    is_redeploy_pending() == True, draining open contracts for real, and
    calling trigger_redeploy() once that's done (which starts the next
    interval).
    """
    global _pending, _last_scheduled_at

    while True:
        try:
            interval_hours = getattr(config, "REDEPLOY_INTERVAL_HOURS", 3)
            interval_secs = interval_hours * 3600
            _last_scheduled_at = time.time()

            next_fire = datetime.fromtimestamp(
                _last_scheduled_at + interval_secs, tz=timezone.utc
            )
            logger.info(
                f"REDEPLOY SCHEDULER: next redeploy at {next_fire.isoformat()} "
                f"({interval_hours}h from now, rolling) — sleeping {interval_secs:.0f}s"
            )
            await asyncio.sleep(interval_secs)

            _pending = True
            logger.warning(
                f"REDEPLOY DUE: {interval_hours}h rolling interval elapsed — "
                f"waiting for bot_engine to drain open contracts before "
                f"actually redeploying"
            )

            # Wait here until bot_engine.py's _settle_loop() confirms the
            # drain completed and calls trigger_redeploy() (which clears
            # _pending). Poll rather than block indefinitely so a stuck
            # drain doesn't wedge this loop forever — just keep logging.
            waited = 0
            while _pending:
                await asyncio.sleep(30)
                waited += 30
                if waited % 600 == 0:
                    logger.warning(
                        f"REDEPLOY STILL PENDING: {waited}s since due — "
                        f"bot_engine is still draining open contracts "
                        f"rather than wiping their bookkeeping"
                    )

            # Loop repeats: the next interval is measured from HERE (i.e.
            # from the actual redeploy/resumption), not from the previous
            # fire time — this is what makes it a rolling interval instead
            # of a fixed-clock schedule that could drift or double-fire if
            # a drain ran long.

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(f"restart_scheduler.run_scheduler: {exc}")
            await asyncio.sleep(60)


def trigger_redeploy() -> None:
    """
    Fires the Render deploy hook (if configured) and clears the pending
    flag. Called by bot_engine.py ONLY after every open contract has been
    actively, confirmably closed.
    """
    global _pending, _last_triggered_at

    hook_url = getattr(config, "RENDER_DEPLOY_HOOK_URL", "")
    _last_triggered_at = time.time()

    if not hook_url:
        logger.warning(
            "trigger_redeploy() called but RENDER_DEPLOY_HOOK_URL is not "
            "set — clearing pending flag without actually redeploying"
        )
        _pending = False
        return

    async def _fire():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(hook_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    body = await resp.text()
                    if resp.status in (200, 201, 202):
                        logger.info(f"REDEPLOY HOOK FIRED: HTTP {resp.status}")
                    else:
                        logger.error(
                            f"REDEPLOY HOOK FAILED: HTTP {resp.status} — {body[:500]}"
                        )
        except Exception as exc:
            logger.error(f"REDEPLOY HOOK ERROR: {exc}")

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_fire())
    except RuntimeError:
        # No running loop — fall back to a synchronous best-effort call.
        try:
            import requests
            resp = requests.post(hook_url, timeout=20)
            logger.info(f"REDEPLOY HOOK FIRED (sync): HTTP {resp.status_code}")
        except Exception as exc:
            logger.error(f"REDEPLOY HOOK ERROR (sync fallback): {exc}")

    _pending = False


def start_restart_scheduler():
    """
    Entry point called by main.py at startup (unchanged name/contract from
    before this fix — only the scheduling logic inside run_scheduler()
    changed, per Implementation Brief v2, Fix G).

    Starts run_scheduler() as a background asyncio task if a loop is
    already running (the normal case — main.py calls this from inside its
    async startup), or spins up a dedicated background thread with its
    own event loop if called from synchronous code before any loop
    exists. Safe to call exactly once at process startup.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(run_scheduler())
        logger.info("restart_scheduler: started on the running event loop")
        return
    except RuntimeError:
        pass

    import threading

    def _thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_scheduler())
        except Exception as exc:
            logger.error(f"restart_scheduler thread crashed: {exc}")

    t = threading.Thread(target=_thread_target, name="restart-scheduler", daemon=True)
    t.start()
    logger.info("restart_scheduler: started on a dedicated background thread")

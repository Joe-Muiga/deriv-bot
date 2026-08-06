"""
restart_scheduler.py – 6-hourly Kenya-midnight-anchored redeploy scheduler.

Implementation Brief v2, Requirement 2 / Fix G. Widened from once-daily to
every REDEPLOY_INTERVAL_HOURS (config.py, default 6) on request, still
anchored to 00:00 Africa/Nairobi — so with the default that's four fires a
day at 00:00 / 06:00 / 12:00 / 18:00 EAT (UTC+3, no DST) instead of one.

Public interface (unchanged contract with bot_engine.py):
  - is_redeploy_pending() -> bool
        True once the timer has fired and a redeploy is due.
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
        The timer loop itself. Started as a background task by
        bot_engine.py's run() alongside its other loops.

Draining coordination lives entirely in bot_engine.py (it owns the open
contracts and the reconciliation / Multiplier max-hold machinery) — this
module only owns the schedule and the actual deploy-hook trigger.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback, not expected here
    ZoneInfo = None

import aiohttp

import config

logger = logging.getLogger(__name__)

_REDEPLOY_TIMEZONE = getattr(config, "REDEPLOY_TIMEZONE", "Africa/Nairobi")

# How often the timer fires, anchored to local midnight — e.g. 6 means
# 00:00/06:00/12:00/18:00 local. Any positive value works (doesn't need
# to divide 24 evenly); see _next_scheduled_fire().
_REDEPLOY_INTERVAL_HOURS = getattr(config, "REDEPLOY_INTERVAL_HOURS", 6)

# ── Module-level scheduler state ────────────────────────────────────────────
_pending: bool = False
_last_triggered_at: float = 0.0
_last_scheduled_at: float = 0.0


def is_redeploy_pending() -> bool:
    """True once the redeploy timer has fired and a redeploy is due but
    hasn't been confirmed-triggered yet."""
    return _pending


def _next_scheduled_fire(now_utc: datetime) -> datetime:
    """
    Given the current UTC time, return the next scheduled redeploy
    instant, expressed in UTC — a boundary every
    _REDEPLOY_INTERVAL_HOURS hours, anchored to local (Africa/Nairobi)
    midnight (00:00/06:00/12:00/18:00 for the default interval of 6).
    Uses zoneinfo so DST-free EAT (UTC+3) stays correct even if that ever
    changes upstream, rather than hardcoding UTC offsets as magic numbers.
    """
    interval = _REDEPLOY_INTERVAL_HOURS
    if ZoneInfo is not None:
        tz = ZoneInfo(_REDEPLOY_TIMEZONE)
        now_local = now_utc.astimezone(tz)
        boundary_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        while boundary_local <= now_local:
            boundary_local += timedelta(hours=interval)
        return boundary_local.astimezone(timezone.utc)

    # Fallback: Africa/Nairobi has no DST — fixed UTC+3 — so this can be
    # computed with a plain offset instead of zoneinfo. Do the "local
    # wall clock" arithmetic on naive datetimes, then reattach UTC at the
    # end so the +3h/-3h offset only ever gets applied once each way.
    now_local_naive = now_utc.replace(tzinfo=None) + timedelta(hours=3)
    boundary_local_naive = now_local_naive.replace(hour=0, minute=0, second=0, microsecond=0)
    while boundary_local_naive <= now_local_naive:
        boundary_local_naive += timedelta(hours=interval)
    return (boundary_local_naive - timedelta(hours=3)).replace(tzinfo=timezone.utc)


async def run_scheduler():
    """
    Sleeps until the next scheduled fire (_next_scheduled_fire), sets the
    pending flag, then repeats. bot_engine.py's _settle_loop() is
    responsible for noticing is_redeploy_pending() == True, draining open
    contracts for real, and calling trigger_redeploy() once that's done.
    """
    global _pending, _last_scheduled_at

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            next_fire = _next_scheduled_fire(now_utc)
            sleep_secs = max(1.0, (next_fire - now_utc).total_seconds())

            logger.info(
                f"REDEPLOY SCHEDULER: next redeploy at {next_fire.isoformat()} "
                f"(every {_REDEPLOY_INTERVAL_HOURS}h from 00:00 "
                f"{_REDEPLOY_TIMEZONE}) — sleeping {sleep_secs:.0f}s"
            )
            await asyncio.sleep(sleep_secs)

            _pending = True
            _last_scheduled_at = time.time()
            logger.warning(
                "REDEPLOY DUE: scheduled redeploy timer fired — "
                "waiting for bot_engine to drain open contracts before "
                "actually redeploying"
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

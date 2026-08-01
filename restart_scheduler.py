"""
restart_scheduler.py – Daily Kenya-midnight redeploy scheduler.

Implementation Brief v2, Requirement 2 / Fix G.

Replaces the old fixed 2-hour redeploy timer with a schedule that fires
exactly once every 24 hours, at 00:00 Africa/Nairobi time (EAT, UTC+3 —
Kenya does not observe DST, so this is a fixed offset year-round).

Public interface (unchanged contract with bot_engine.py):
  - is_redeploy_pending() -> bool
        True once the daily timer has fired and a redeploy is due.
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
        The daily timer loop itself. Started as a background task by
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

# ── Module-level scheduler state ────────────────────────────────────────────
_pending: bool = False
_last_triggered_at: float = 0.0
_last_scheduled_at: float = 0.0


def is_redeploy_pending() -> bool:
    """True once the daily Kenya-midnight timer has fired and a redeploy
    is due but hasn't been confirmed-triggered yet."""
    return _pending


def _next_nairobi_midnight(now_utc: datetime) -> datetime:
    """
    Given the current UTC time, return the next 00:00 Africa/Nairobi
    instant, expressed in UTC. Uses zoneinfo so DST-free EAT (UTC+3) stays
    correct even if that ever changes upstream, rather than hardcoding
    "21:00 UTC" as a magic number.
    """
    if ZoneInfo is not None:
        tz = ZoneInfo(_REDEPLOY_TIMEZONE)
        now_local = now_utc.astimezone(tz)
        next_local_midnight = (now_local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return next_local_midnight.astimezone(timezone.utc)

    # Fallback: Africa/Nairobi has no DST — fixed UTC+3 — so 00:00 EAT is
    # always 21:00 UTC the previous day.
    candidate = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
    if candidate <= now_utc:
        candidate += timedelta(days=1)
    return candidate


async def run_scheduler():
    """
    Sleeps until the next Africa/Nairobi midnight, sets the pending flag,
    then repeats. bot_engine.py's _settle_loop() is responsible for
    noticing is_redeploy_pending() == True, draining open contracts for
    real, and calling trigger_redeploy() once that's done.
    """
    global _pending, _last_scheduled_at

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            next_fire = _next_nairobi_midnight(now_utc)
            sleep_secs = max(1.0, (next_fire - now_utc).total_seconds())

            logger.info(
                f"REDEPLOY SCHEDULER: next redeploy at {next_fire.isoformat()} "
                f"(00:00 {_REDEPLOY_TIMEZONE}) — sleeping {sleep_secs:.0f}s"
            )
            await asyncio.sleep(sleep_secs)

            _pending = True
            _last_scheduled_at = time.time()
            logger.warning(
                "REDEPLOY DUE: daily Kenya-midnight timer fired — "
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

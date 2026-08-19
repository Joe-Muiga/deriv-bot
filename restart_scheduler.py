"""
restart_scheduler.py – Rolling trading-window redeploy scheduler.

User-directed rewrite, Aug 2026: "i would also wish that this bot trades
for 3hrs then it does not place new trades & waits for any trades that
are open to close then the bot automatically redeploys and then starts
to trade afresh this process should be autonomous and repeats itself
continuously forever."

Previously this fired on a fixed clock anchor (originally daily at 00:00
Africa/Nairobi per Implementation Brief v2 Fix G, later widened to every
REDEPLOY_INTERVAL_HOURS but still anchored to that same midnight
boundary). That anchoring is gone: the timer is now purely ROLLING,
measured from the moment trading actually resumes after each redeploy,
not from any fixed wall-clock instant. That's what "trades for 3hrs" as
a repeating cycle actually requires — an anchor-based timer would make
each trading window a different real length depending on how long the
previous drain took.

Public interface (unchanged contract with bot_engine.py):
  - is_redeploy_pending() -> bool
        True once the REDEPLOY_INTERVAL_HOURS timer has fired and a
        redeploy is due. bot_engine.py's _main_loop() checks this to
        pause taking on new trades, and _settle_loop() checks it to know
        when to start draining open contracts before actually
        redeploying.
  - trigger_redeploy() -> None
        Called by bot_engine.py's _settle_loop() ONLY once every open
        contract has been actively, confirmably closed (or the drain
        window has been exceeded and DRAIN_MAX_SECS forces it through —
        see bot_engine.py). Fires the Render deploy hook and clears the
        pending flag.
  - run_scheduler() -> coroutine
        The rolling timer loop itself. Started as a background task by
        bot_engine.py's run() alongside its other loops.

How the "forever" part actually happens: trigger_redeploy() firing the
real Render deploy hook kills this process — main.py starts fresh,
start_restart_scheduler() is called again from scratch, and a brand new
REDEPLOY_INTERVAL_HOURS timer begins counting from that fresh start.
Each redeploy cycle IS the repeat; there's no multi-cycle loop needed
inside a single process for that path. The `while True` below exists for
the other path: if RENDER_DEPLOY_HOOK_URL isn't set (e.g. local/dev),
trigger_redeploy() can't actually restart the process, so it just clears
the pending flag in place — in that case this loop starts the next
REDEPLOY_INTERVAL_HOURS window itself, immediately, inside the same
process, so the cycle still repeats continuously either way.

Draining coordination lives entirely in bot_engine.py (it owns the open
contracts and the reconciliation / Multiplier max-hold machinery) — this
module only owns the schedule and the actual deploy-hook trigger.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

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
    """True once the rolling trading-window timer has fired and a
    redeploy is due but hasn't been confirmed-triggered yet."""
    return _pending


def _fmt_local(ts: float) -> str:
    """Local-time-formatted timestamp for log readability only — display
    concern, does not drive scheduling (see module docstring)."""
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    if ZoneInfo is not None:
        try:
            local = dt_utc.astimezone(ZoneInfo(_REDEPLOY_TIMEZONE))
            return f"{local.isoformat()} ({_REDEPLOY_TIMEZONE})"
        except Exception:
            pass
    return f"{dt_utc.isoformat()} (UTC)"


async def run_scheduler():
    """
    Sleeps for a rolling config.REDEPLOY_INTERVAL_HOURS (default 3) from
    whenever this loop iteration starts, sets the pending flag, then
    waits for bot_engine.py's _settle_loop() to notice
    is_redeploy_pending() == True, actually drain every open contract,
    and call trigger_redeploy(). Once that clears the pending flag, the
    loop starts the next window immediately — see module docstring for
    why that's only reachable at all when RENDER_DEPLOY_HOOK_URL isn't
    set; the normal case is a real process restart re-entering this
    function fresh instead.
    """
    global _pending, _last_scheduled_at

    interval_hours = float(getattr(config, "REDEPLOY_INTERVAL_HOURS", 3))
    interval_secs = max(60.0, interval_hours * 3600.0)

    while True:
        try:
            window_start = time.time()
            next_fire = window_start + interval_secs
            logger.info(
                f"REDEPLOY SCHEDULER: trading window open for "
                f"{interval_hours:g}h — next redeploy due at "
                f"{_fmt_local(next_fire)}"
            )
            await asyncio.sleep(interval_secs)

            _pending = True
            _last_scheduled_at = time.time()
            logger.warning(
                f"REDEPLOY DUE: {interval_hours:g}h rolling trading-window "
                f"timer fired — no new entries until bot_engine finishes "
                f"draining every open contract and calls trigger_redeploy()"
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
    actively, confirmably closed. If the hook fires, the whole process
    restarts and the next rolling window begins fresh from process start
    (see module docstring); if no hook is configured, clearing the
    pending flag here lets run_scheduler()'s own while-loop start the
    next window immediately instead.
    """
    global _pending, _last_triggered_at

    hook_url = getattr(config, "RENDER_DEPLOY_HOOK_URL", "")
    _last_triggered_at = time.time()

    if not hook_url:
        logger.warning(
            "trigger_redeploy() called but RENDER_DEPLOY_HOOK_URL is not "
            "set — clearing pending flag without actually redeploying; "
            "the next rolling window starts immediately in this same "
            "process instead of via a fresh restart"
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
    Entry point called by main.py at startup (unchanged name/contract).
    Starts run_scheduler() as a background asyncio task if a loop is
    already running (the normal case — main.py calls this from inside its
    async startup), or spins up a dedicated background thread with its
    own event loop if called from synchronous code before any loop
    exists. Safe to call exactly once at process startup — including once
    per fresh process after every redeploy, which is what makes the
    rolling window repeat "continuously forever" across restarts.
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

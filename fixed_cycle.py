"""
fixed_cycle.py – fixed, never-ending two-leg trading cycle.

Spec (Sep 2026, chat-requested): replaces both strategy_cycle.py's
balance-trend Donkey ORIGINAL/RAW switch and profit_cycle.py's
profit-target cycle. Neither balance, profit, drawdown, nor time of day
has any say in this anymore — the pattern is purely fixed-length:

  1. Trade for config.FIXED_CYCLE_LEG_MINUTES ("leg 1"). config.
     REDEPLOY_INTERVAL_HOURS is already set to this same 5 minutes, so
     restart_scheduler.py's existing rolling-redeploy timer IS leg 1's
     clock — this module doesn't need a separate one for it.
  2. Ordinary "leg 1 -> leg 2" redeploy: drain open contracts, redeploy,
     resume trading immediately — no deliberate disconnect beyond the
     redeploy itself.
  3. Trade for FIXED_CYCLE_LEG_MINUTES again ("leg 2").
  4. Drain open contracts, then disconnect from Deriv entirely for
     config.FIXED_CYCLE_COOLDOWN_MINUTES (health checks only), then
     redeploy back into a fresh leg 1.
  5. Repeat forever.

PERSISTENCE — same reasoning as strategy_cycle.py / profit_cycle.py
before it: Render's disk on this plan is ephemeral, every redeploy is a
brand-new container, so which leg is active has to survive that redeploy
somewhere durable. State is written into THIS SERVICE'S OWN Render
environment variables via Render's public API (PUT /v1/services/
{serviceId}/env-vars/{envVarKey}) every time it changes, and read back
out of os.environ at boot. Needs the same two env vars the two modules
this replaces needed:

  RENDER_API_KEY     – Render Dashboard -> Account Settings -> API Keys
  RENDER_SERVICE_ID  – the "srv-xxxxxxxxxxxxxxxxxxxx" id in this
                        service's Settings-page URL

Without both set, persistence is impossible on Render's architecture no
matter what this module does — see _warn_persistence_not_configured().

Persisted keys (plain env vars on the service):
  FIXED_PHASE            "trading" | "cooldown"
  FIXED_LEG              "1" | "2" — which trading leg is active/next
  FIXED_COOLDOWN_UNTIL    epoch seconds the current cooldown ends at

Public interface:
  load_state()                          -> {"phase", "leg", "cooldown_until"}
  in_cooldown_at_boot()                 -> cooldown_until epoch, or None
  clear_cooldown_and_resume_trading()   -> flip phase back to "trading",
                                            leg back to "1"
  get_or_init_leg()                     -> ensures FIXED_LEG is set
                                            (bootstrap default "1" on the
                                            very first run ever)
  on_ordinary_redeploy_due()            -> called every settle tick
                                            while restart_scheduler.
                                            is_redeploy_pending() is True;
                                            requests a cooldown instead
                                            of letting the redeploy go
                                            through plainly once leg "2"
                                            is the one finishing
  advance_leg_after_redeploy()          -> called right after an
                                            ordinary (non-cooldown)
                                            redeploy actually fires;
                                            bumps leg "1" -> "2"
  request_cooldown() / is_cooldown_requested()
                                         -> pending-flag, mirrors
                                            restart_scheduler.py's
                                            is_redeploy_pending() pattern
  enter_cooldown_now()                  -> persists the cooldown window
                                            + resets leg to "1", starts
                                            the supervisor thread,
                                            returns cooldown_until
  start_cooldown_supervisor(until)      -> the thread that waits out the
                                            cooldown then fires the
                                            Render deploy hook
"""

import asyncio
import logging
import os
import threading
import time
from typing import Optional

import aiohttp
import requests

import config

logger = logging.getLogger(__name__)

# ── Module-level state ──────────────────────────────────────────────────────
_cooldown_requested:    bool  = False
_persistence_warned_at: float = 0.0


def _push_dashboard_flag(**kwargs) -> None:
    """Soft/optional dashboard visibility — same guarded-lazy-import
    pattern strategy_cycle.py / profit_cycle.py used. Never let a
    missing/broken keep_alive break the cycle logic itself."""
    try:
        import keep_alive
        keep_alive.update_status(**kwargs)
    except Exception:
        pass


def _read_float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _warn_persistence_not_configured() -> None:
    global _persistence_warned_at
    now = time.time()
    if now - _persistence_warned_at < 900:   # at most once every 15 min
        return
    _persistence_warned_at = now
    logger.critical(
        "\n" + "=" * 78 + "\n"
        "FIXED-CYCLE PERSISTENCE NOT CONFIGURED\n"
        "RENDER_API_KEY and/or RENDER_SERVICE_ID are not set. Which leg "
        "is active, and the cooldown deadline, will only live in this "
        "process's own environment — they WILL be lost on the next "
        "redeploy (Render's disk here is ephemeral), so the bot will "
        "treat every redeploy as leg 1 instead of tracking the 2-leg "
        "pattern across the whole run. Set both in the Render dashboard "
        "to fix this:\n"
        "  RENDER_API_KEY    -> Account Settings -> API Keys\n"
        "  RENDER_SERVICE_ID -> the srv-xxxxxxxx id in this service's own "
        "Settings-page URL\n" + "=" * 78
    )
    _push_dashboard_flag(fixed_cycle_persistence_missing=True)


def _persist_env_vars(mapping: dict) -> None:
    """Reflects `mapping` in this process's own os.environ immediately,
    and best-effort pushes each key to this service's Render env vars so
    a FUTURE deploy starts from them too. Same async-task-with-sync-
    fallback shape as strategy_cycle.py's / profit_cycle.py's helper of
    the same name."""
    for key, value in mapping.items():
        os.environ[key] = str(value)

    api_key    = getattr(config, "RENDER_API_KEY", "")
    service_id = getattr(config, "RENDER_SERVICE_ID", "")
    if not api_key or not service_id:
        _warn_persistence_not_configured()
        return

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    async def _push():
        async with aiohttp.ClientSession(headers=headers) as session:
            for key, value in mapping.items():
                url = f"https://api.render.com/v1/services/{service_id}/env-vars/{key}"
                try:
                    async with session.put(
                        url, json={"value": str(value)},
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status in (200, 201):
                            logger.info(f"FIXED-CYCLE: persisted {key}={value} ✓")
                        else:
                            body = await resp.text()
                            logger.error(
                                f"FIXED-CYCLE: failed to persist {key} — "
                                f"HTTP {resp.status}: {body[:300]}")
                except Exception as exc:
                    logger.error(f"FIXED-CYCLE: error persisting {key}: {exc}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_push())
    except RuntimeError:
        # No running loop (e.g. called from main.py's synchronous boot
        # path) — fall back to plain blocking requests, one call per key.
        for key, value in mapping.items():
            url = f"https://api.render.com/v1/services/{service_id}/env-vars/{key}"
            try:
                resp = requests.put(
                    url, json={"value": str(value)}, headers=headers, timeout=20)
                if resp.status_code in (200, 201):
                    logger.info(f"FIXED-CYCLE: persisted {key}={value} ✓ (sync)")
                else:
                    logger.error(
                        f"FIXED-CYCLE: failed to persist {key} (sync) — "
                        f"HTTP {resp.status_code}: {resp.text[:300]}")
            except Exception as exc:
                logger.error(f"FIXED-CYCLE: error persisting {key} (sync): {exc}")


# ── State loading ────────────────────────────────────────────────────────────

def load_state() -> dict:
    phase = os.environ.get("FIXED_PHASE", "trading").strip().lower()
    if phase not in ("trading", "cooldown"):
        phase = "trading"
    leg = os.environ.get("FIXED_LEG", "1").strip()
    if leg not in ("1", "2"):
        leg = "1"
    return {
        "phase":          phase,
        "leg":            leg,
        "cooldown_until": _read_float_env("FIXED_COOLDOWN_UNTIL", 0.0),
    }


def in_cooldown_at_boot() -> Optional[float]:
    """Returns the cooldown_until epoch if this process booted mid-
    cooldown (main.py must NOT start the bot / connect to Deriv in that
    case), or None if it's clear to trade — either genuinely trading
    already, or a cooldown that has already elapsed (this process IS the
    post-cooldown redeploy)."""
    state = load_state()
    if state["phase"] == "cooldown" and state["cooldown_until"] > time.time():
        return state["cooldown_until"]
    return None


def clear_cooldown_and_resume_trading() -> None:
    """Called by main.py at boot when the persisted phase is 'cooldown'
    but the deadline has already passed — i.e. this process is the
    redeploy the cooldown supervisor triggered. Flips the phase back to
    'trading' and resets the leg counter to '1' for a fresh pair of
    legs."""
    logger.info("FIXED-CYCLE: cooldown window elapsed — resuming trading on a fresh leg 1")
    _persist_env_vars({
        "FIXED_PHASE":           "trading",
        "FIXED_LEG":             "1",
        "FIXED_COOLDOWN_UNTIL":  "0",
    })


# ── Leg tracking ─────────────────────────────────────────────────────────────

def get_or_init_leg() -> str:
    """
    Called once by bot_engine.run() right after connecting, purely for
    logging/bootstrap — unlike strategy_cycle.py's / profit_cycle.py's
    equivalents this doesn't need a balance snapshot, since nothing here
    is balance-driven. Makes sure FIXED_LEG is actually set (bootstrap
    default "1" on the very first run ever) and returns it.
    """
    state = load_state()
    if "FIXED_LEG" not in os.environ:
        _persist_env_vars({"FIXED_PHASE": "trading", "FIXED_LEG": state["leg"]})
    logger.info(f"FIXED-CYCLE: this run is leg {state['leg']} of 2")
    return state["leg"]


# ── Pending-cooldown flag (mirrors restart_scheduler.py's
#    is_redeploy_pending() pattern) ─────────────────────────────────────────

def is_cooldown_requested() -> bool:
    return _cooldown_requested


def request_cooldown(reason: str) -> None:
    global _cooldown_requested
    if not _cooldown_requested:
        _cooldown_requested = True
        logger.warning(
            f"FIXED-CYCLE: COOLDOWN REQUESTED ({reason}) — no new trades "
            f"will open while open contracts drain")
        _push_dashboard_flag(fixed_cycle_cooldown_reason=reason)


def on_ordinary_redeploy_due() -> None:
    """
    Called by bot_engine._settle_loop() on every settle tick while
    restart_scheduler.is_redeploy_pending() is True — mirrors exactly
    where strategy_cycle.is_starting_drawdown_breached() / profit_cycle.
    is_starting_drawdown_breached() used to be checked. If the leg that's
    about to redeploy is leg "2", this is the end of the two-leg pattern:
    request a cooldown instead of letting the ordinary redeploy go
    through. If it's leg "1", there's nothing to do here — the leg
    advances to "2" in advance_leg_after_redeploy() once the ordinary
    redeploy actually fires. No-ops once a cooldown is already requested.
    """
    if is_cooldown_requested():
        return
    state = load_state()
    if state["leg"] == "2":
        request_cooldown("second 5-minute leg complete")


def advance_leg_after_redeploy() -> None:
    """
    Called by bot_engine._settle_loop() immediately after restart_
    scheduler.trigger_redeploy() actually fires for the PLAIN "leg 1 ->
    leg 2" redeploy path (i.e. only reached when on_ordinary_redeploy_due
    did NOT request a cooldown this tick, meaning the leg that just
    finished was "1"). Persists leg "2" so the fresh deploy that comes up
    trades leg 2 instead of restarting leg 1.
    """
    state = load_state()
    if state["leg"] == "1":
        logger.info("FIXED-CYCLE: leg 1 complete — advancing to leg 2")
        _persist_env_vars({"FIXED_LEG": "2"})


def enter_cooldown_now() -> float:
    """
    Called by bot_engine._settle_loop() ONLY once every open contract has
    been actively, confirmably closed. Persists the cooldown window (so
    it survives the redeploy that ends it) and resets the leg counter
    back to "1" so the post-cooldown deploy starts a fresh pair of legs,
    then starts the supervisor thread that waits it out and fires the
    Render deploy hook. Returns the cooldown_until epoch.
    """
    global _cooldown_requested

    cooldown_mins  = getattr(config, "FIXED_CYCLE_COOLDOWN_MINUTES", 5)
    cooldown_until = time.time() + cooldown_mins * 60

    _persist_env_vars({
        "FIXED_PHASE":          "cooldown",
        "FIXED_COOLDOWN_UNTIL": f"{cooldown_until:.0f}",
        "FIXED_LEG":            "1",
    })
    _cooldown_requested = False
    _push_dashboard_flag(
        fixed_cycle_phase="cooldown",
        fixed_cycle_cooldown_until=cooldown_until,
    )
    logger.warning(
        f"FIXED-CYCLE: both legs complete — {cooldown_mins:.0f} min "
        f"cooldown, then back to a fresh leg 1")
    start_cooldown_supervisor(cooldown_until)
    return cooldown_until


# ── Cooldown supervisor — dedicated thread, deliberately NOT tied to the
#    bot's asyncio event loop, same reasoning as strategy_cycle.py's /
#    profit_cycle.py's supervisor of the same shape (that loop is torn
#    down the moment run() returns and asyncio.run() in main.py's
#    _run_bot() exits). ───────────────────────────────────────────────────

def _fire_deploy_hook() -> None:
    hook_url = os.environ.get("RENDER_DEPLOY_HOOK_URL", "") or \
        getattr(config, "RENDER_DEPLOY_HOOK_URL", "")

    while not hook_url:
        logger.critical(
            "\n" + "=" * 78 + "\n"
            "COOLDOWN OVER BUT RENDER_DEPLOY_HOOK_URL IS NOT SET\n"
            "Cannot redeploy to resume trading on a fresh leg 1. Add it "
            "in the Render dashboard — doing so restarts this service "
            "itself, which will pick the reset leg counter up from "
            "there. Rechecking every 5 min.\n" + "=" * 78
        )
        _push_dashboard_flag(fixed_cycle_redeploy_hook_missing=True)
        time.sleep(300)
        hook_url = os.environ.get("RENDER_DEPLOY_HOOK_URL", "") or \
            getattr(config, "RENDER_DEPLOY_HOOK_URL", "")

    try:
        resp = requests.post(hook_url, timeout=20)
        if resp.status_code in (200, 201, 202):
            logger.info(f"FIXED-CYCLE: redeploy hook fired ✓ HTTP {resp.status_code}")
            _push_dashboard_flag(fixed_cycle_redeploy_hook_missing=False)
        else:
            logger.error(
                f"FIXED-CYCLE: redeploy hook FAILED — HTTP "
                f"{resp.status_code}: {resp.text[:300]}")
    except Exception as exc:
        logger.error(f"FIXED-CYCLE: redeploy hook ERROR: {exc}")


def _cooldown_supervisor_thread(cooldown_until: float) -> None:
    remaining = cooldown_until - time.time()
    logger.warning(
        f"FIXED-CYCLE COOLDOWN: NOT connecting to Deriv for the next "
        f"{max(0.0, remaining) / 60:.1f} min — health checks only, "
        f"staying alive on Render via the self-ping thread")

    next_log_at = 0.0
    while True:
        now       = time.time()
        remaining = cooldown_until - now
        _push_dashboard_flag(
            fixed_cycle_phase="cooldown",
            fixed_cycle_cooldown_remaining_secs=max(0, int(remaining)),
        )
        if remaining <= 0:
            break
        if now >= next_log_at:
            logger.info(f"FIXED-CYCLE COOLDOWN: {remaining / 60:.1f} min remaining")
            next_log_at = now + 60
        time.sleep(min(5.0, max(0.1, remaining)))

    logger.warning(
        "FIXED-CYCLE COOLDOWN COMPLETE — firing Render deploy hook to "
        "resume trading on a fresh leg 1")
    _fire_deploy_hook()


def start_cooldown_supervisor(cooldown_until: float) -> None:
    t = threading.Thread(
        target=_cooldown_supervisor_thread,
        args=(cooldown_until,),
        name="fixed-cycle-cooldown",
        daemon=True,
    )
    t.start()

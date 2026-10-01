"""
profit_pause.py — pause trading for a random 11-18 min once session profit hits the target
for the account's stake tier (see balance_tiers.py for the table).

Session = from the moment trading (re)starts after a pause until the next
pause. The session's starting balance is persisted in the env var
PROFIT_PAUSE_START_BALANCE (same Render-env persistence as fixed_cycle.py), so
the 5-minute leg redeploys do NOT reset it.

Target basis (config.PROFIT_PAUSE_TARGET_BASIS):
  "start"   (default) tier of the session's STARTING balance fixes the $ target
            for the whole session, even if balance climbs into a higher band.
  "current" tier of the live balance is used on every check.

When the target is hit this calls fixed_cycle.request_cooldown(fixed_mins=<random 11-18>).
The existing drain-then-disconnect machinery in bot_engine._settle_loop does
the rest; enter_cooldown_now() then calls on_pause_entered() to clear the
session so the next deploy starts a fresh one.
"""

import logging
import os
import random
from typing import Optional

import config
import balance_tiers

logger = logging.getLogger(__name__)

ENV_KEY = "PROFIT_PAUSE_START_BALANCE"
_start_balance: Optional[float] = None
_fired: bool = False


def _enabled() -> bool:
    return bool(getattr(config, "PROFIT_PAUSE_ENABLED", True))


def _persist(value: float) -> None:
    try:
        import fixed_cycle
        fixed_cycle._persist_env_vars({ENV_KEY: f"{value:.4f}"})
    except Exception as exc:
        os.environ[ENV_KEY] = f"{value:.4f}"
        logger.warning(f"PROFIT-PAUSE: could not persist session start: {exc}")


def on_boot(balance: float) -> float:
    """Call once after connecting. Restores the persisted session start, or
    starts a fresh session at `balance`."""
    global _start_balance, _fired
    _fired = False
    try:
        saved = float(os.environ.get(ENV_KEY, "0") or 0)
    except ValueError:
        saved = 0.0
    if saved > 0:
        _start_balance = saved
        logger.info(f"PROFIT-PAUSE: resumed session (start ${saved:.2f}, "
                    f"now ${balance:.2f})")
    else:
        _start_balance = float(balance)
        _persist(_start_balance)
        logger.info(f"PROFIT-PAUSE: new session started at ${balance:.2f}")
    return _start_balance


def target_for_session(balance: float) -> float:
    basis = str(getattr(config, "PROFIT_PAUSE_TARGET_BASIS", "start")).lower()
    ref = _start_balance if (basis == "start" and _start_balance) else balance
    return balance_tiers.profit_target_for_balance(ref)


def check(balance: float) -> bool:
    """Call on every settle tick with the live balance. Returns True the one
    time the pause is requested."""
    global _fired
    if not _enabled() or _fired or _start_balance is None:
        return False
    if balance is None or balance <= 0:
        return False
    profit = balance - _start_balance
    target = target_for_session(balance)
    if profit < target:
        return False
    _fired = True
    lo = float(getattr(config, "PROFIT_PAUSE_MIN_MINUTES", balance_tiers.PAUSE_MIN_MINUTES))
    hi = float(getattr(config, "PROFIT_PAUSE_MAX_MINUTES", balance_tiers.PAUSE_MAX_MINUTES))
    if hi < lo:
        lo, hi = hi, lo
    mins = random.uniform(lo, hi)     # drawn once per pause, then persisted as a deadline
    reason = (f"profit target hit (+${profit:.2f} >= ${target:.2f}, {mins:.1f} min pause, "
              f"stake tier ${balance_tiers.tier_for_balance(_start_balance).stake:g})")
    try:
        import fixed_cycle
        fixed_cycle.request_cooldown(reason, fixed_mins=mins)
    except Exception as exc:
        logger.error(f"PROFIT-PAUSE: could not request pause: {exc}")
        _fired = False
        return False
    return True


def on_pause_entered() -> dict:
    """Called from fixed_cycle.enter_cooldown_now() when the pause actually
    begins (all contracts closed). Returns env vars to persist: clears the
    session start so the post-pause deploy opens a new session, and resets
    the donkey guard's session so stop-loss counting starts fresh."""
    global _start_balance, _fired
    if not _fired:
        return {}
    _start_balance = None
    _fired = False
    try:
        from donkey_guard import get_guard
        get_guard().reset_session()
    except Exception as exc:
        logger.warning(f"PROFIT-PAUSE: guard reset failed: {exc}")
    return {ENV_KEY: "0"}

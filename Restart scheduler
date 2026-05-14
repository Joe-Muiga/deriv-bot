"""
restart_scheduler.py – Triggers a fresh Render deployment every 15 minutes.

Why: The bot stops generating trades after several executions; a fresh deploy
reliably restores profitable trading without any code changes.

Behaviour:
  - Runs in a daemon thread (never blocks the bot or Flask).
  - Waits 15 minutes, then POSTs to RENDER_DEPLOY_HOOK_URL.
  - On failure, logs the error and retries on the next 15-minute tick.
  - If RENDER_DEPLOY_HOOK_URL is not set, logs a one-time warning and exits
    (safe no-op for local development).
"""

import logging
import threading
import time

import requests
import config

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 900  # 15 minutes


def _scheduler_loop() -> None:
    url = config.RENDER_DEPLOY_HOOK_URL

    if not url:
        logger.warning(
            "RENDER_DEPLOY_HOOK_URL is not set – "
            "auto-redeploy scheduler is disabled. "
            "Add the env var in Render → Environment to enable it."
        )
        return

    logger.info(
        f"Restart scheduler started – will trigger a fresh deploy "
        f"every {_INTERVAL_SECONDS // 60} minutes."
    )

    while True:
        time.sleep(_INTERVAL_SECONDS)

        logger.info("Restart scheduler: POSTing to Render deploy hook …")
        try:
            response = requests.post(url, timeout=15)
            if response.ok:
                logger.info(
                    f"Restart scheduler: deploy triggered successfully "
                    f"(HTTP {response.status_code}). "
                    "Render will build and redeploy the bot shortly."
                )
            else:
                logger.error(
                    f"Restart scheduler: deploy hook returned an unexpected status – "
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )
        except requests.RequestException as exc:
            logger.error(
                f"Restart scheduler: POST to deploy hook failed – {exc}. "
                "Will retry on the next 15-minute tick."
            )


def start_restart_scheduler() -> None:
    """Spawn the scheduler as a background daemon thread."""
    t = threading.Thread(
        target=_scheduler_loop,
        name="restart-scheduler",
        daemon=True,
    )
    t.start()
    logger.info("Restart scheduler thread started ✓")

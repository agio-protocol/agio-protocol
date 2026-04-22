"""Production reconciliation service with email alerts."""
import asyncio
import logging
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone

from ..core.config import settings
import src.core.database as db_mod

import redis.asyncio as aioredis
import src.core.redis as redis_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("reconciler_prod")

redis_mod.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

from .reconciliation_service import run_reconciliation

ALERT_EMAIL = os.getenv("ALERT_EMAIL", "jeffrey_wylie@yahoo.com")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
RECONCILE_INTERVAL = 300


def send_alert(subject: str, body: str):
    if not SMTP_HOST or not SMTP_USER:
        logger.warning(f"ALERT (no SMTP configured): {subject}\n{body}")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = f"[AGIO ALERT] {subject}"
        msg["From"] = SMTP_USER
        msg["To"] = ALERT_EMAIL
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info(f"Alert email sent: {subject}")
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")


async def run_service():
    logger.info(f"Production reconciler started. Interval: {RECONCILE_INTERVAL}s. Alerts: {ALERT_EMAIL}")
    consecutive_failures = 0

    while True:
        try:
            result = await run_reconciliation()

            if result.ok:
                consecutive_failures = 0
                logger.info(f"Reconciliation PASS — {result.checks_passed} checks")
            else:
                consecutive_failures += 1
                logger.error(f"Reconciliation FAIL — {result.checks_failed} failures (consecutive: {consecutive_failures})")

                if consecutive_failures >= 2:
                    send_alert(
                        "Reconciliation Failed",
                        f"AGIO reconciliation has failed {consecutive_failures} times in a row.\n\n"
                        f"Failures:\n" +
                        "\n".join(f"- {d['check']}: {d['actual']}" for d in result.discrepancies) +
                        f"\n\nTimestamp: {datetime.now(timezone.utc).isoformat()}\n"
                        f"Action: Check admin dashboard at admin.agiotage.finance"
                    )
                    from ..core.redis import redis_client
                    await redis_client.set("AGIO:payments_paused", "1")
                    await redis_client.set("AGIO:pause_reason", "Reconciliation mismatch")

        except Exception as e:
            logger.error(f"Reconciler error: {e}", exc_info=True)

        await asyncio.sleep(RECONCILE_INTERVAL)


asyncio.run(run_service())

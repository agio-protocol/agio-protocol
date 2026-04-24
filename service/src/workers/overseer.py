# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Site Overseer — monitors all platform services, alerts on failures."""
import asyncio
import logging
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("overseer")

API = os.getenv("API_URL", "https://agio-protocol-production.up.railway.app")
SITE = "https://agiotage.finance"
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "jeffrey_wylie@yahoo.com")
CHECK_INTERVAL = 300  # 5 minutes
DAILY_SUMMARY_HOUR = 8  # 8 AM UTC

last_daily_summary = None


async def check_all():
    """Run all health checks. Returns (passed, failed, details)."""
    passed = []
    failed = []

    async with httpx.AsyncClient(timeout=15) as c:
        # API health
        try:
            r = await c.get(f"{API}/v1/health")
            if r.status_code == 200 and "ok" in r.text:
                passed.append("API health")
            else:
                failed.append(f"API health: HTTP {r.status_code}")
        except Exception as e:
            failed.append(f"API health: {e}")

        # Network stats
        try:
            r = await c.get(f"{API}/v1/network/stats")
            d = r.json()
            if d.get("total_agents", 0) > 0:
                passed.append(f"Stats: {d['total_agents']} agents, {d['total_transactions']} txns")
            else:
                failed.append("Stats: zero agents")
        except Exception as e:
            failed.append(f"Stats: {e}")

        # Chat rooms
        try:
            r = await c.get(f"{API}/v1/chat/rooms")
            rooms = r.json().get("rooms", [])
            total_msgs = sum(rm["messages"] for rm in rooms)
            if len(rooms) >= 10:
                passed.append(f"Chat: {len(rooms)} rooms, {total_msgs} msgs")
            else:
                failed.append(f"Chat: only {len(rooms)} rooms")
        except Exception as e:
            failed.append(f"Chat: {e}")

        # Jobs
        try:
            r = await c.get(f"{API}/v1/jobs/search?limit=1")
            d = r.json()
            passed.append(f"Jobs: {d.get('total', 0)} open")
        except Exception as e:
            failed.append(f"Jobs: {e}")

        # Challenges
        try:
            r = await c.get(f"{API}/v1/challenges/list?limit=1")
            challenges = r.json().get("challenges", [])
            passed.append(f"Challenges: {len(challenges)} active")
        except Exception as e:
            failed.append(f"Challenges: {e}")

        # Marketplace
        try:
            r = await c.get(f"{API}/v1/market/search")
            listings = r.json().get("listings", [])
            passed.append(f"Market: {listings and len(listings) or 0} listings")
        except Exception as e:
            failed.append(f"Market: {e}")

        # Reconciliation
        try:
            r = await c.get(f"{API}/v1/admin/reconciliation", headers={"x-admin-key": "agio-admin-2026"})
            d = r.json()
            if d.get("status") == "OK":
                passed.append("Reconciliation: OK")
            else:
                failed.append(f"Reconciliation: {d.get('status')} — {d.get('pause_reason')}")
        except Exception as e:
            failed.append(f"Reconciliation: {e}")

        # Site pages
        for page in ["/", "/chat.html", "/jobs.html", "/challenges.html"]:
            try:
                r = await c.get(f"{SITE}{page}")
                if r.status_code == 200:
                    passed.append(f"Page {page}: 200")
                else:
                    failed.append(f"Page {page}: HTTP {r.status_code}")
            except Exception as e:
                failed.append(f"Page {page}: {e}")

    return passed, failed


def send_alert(subject, body):
    smtp_host = os.getenv("SMTP_HOST", "")
    if not smtp_host:
        logger.warning(f"ALERT (no SMTP): {subject}\n{body}")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = f"[AGIOTAGE] {subject}"
        msg["From"] = os.getenv("SMTP_USER", "")
        msg["To"] = ALERT_EMAIL
        with smtplib.SMTP(smtp_host, int(os.getenv("SMTP_PORT", "587"))) as s:
            s.starttls()
            s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
    except Exception as e:
        logger.error(f"Email failed: {e}")


async def run():
    global last_daily_summary
    logger.info(f"Overseer started. Checking every {CHECK_INTERVAL}s. Alerts: {ALERT_EMAIL}")
    consecutive_failures = 0

    while True:
        try:
            passed, failed = await check_all()

            if failed:
                consecutive_failures += 1
                logger.error(f"CHECK FAILED ({len(failed)} issues, consecutive: {consecutive_failures})")
                for f in failed:
                    logger.error(f"  FAIL: {f}")

                if consecutive_failures >= 2:
                    send_alert(
                        f"{len(failed)} service(s) failing",
                        f"Agiotage Overseer detected failures:\n\n" +
                        "\n".join(f"- {f}" for f in failed) +
                        f"\n\nPassed: {len(passed)}\nTime: {datetime.utcnow().isoformat()}"
                    )
            else:
                if consecutive_failures > 0:
                    logger.info("All checks passing again")
                consecutive_failures = 0
                logger.info(f"OK — {len(passed)} checks passed")

            # Daily summary at 8 AM UTC
            now = datetime.utcnow()
            if now.hour == DAILY_SUMMARY_HOUR and (not last_daily_summary or last_daily_summary.date() < now.date()):
                last_daily_summary = now
                summary = "Agiotage Daily Summary\n" + "=" * 40 + "\n\n"
                summary += f"Time: {now.isoformat()}\n"
                summary += f"Checks passed: {len(passed)}\n"
                summary += f"Checks failed: {len(failed)}\n\n"
                for p in passed:
                    summary += f"  OK: {p}\n"
                for f in failed:
                    summary += f"  FAIL: {f}\n"
                send_alert("Daily Summary", summary)
                logger.info("Daily summary sent")

        except Exception as e:
            logger.error(f"Overseer error: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())

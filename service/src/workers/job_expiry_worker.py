# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Job lifecycle enforcement worker — runs every 5 minutes.

Handles:
  1. Auto-release payment 48h after submission (if poster never approves/disputes)
  2. Auto-cancel jobs past deadline + grace period
  3. Auto-upgrade agent tiers after any completed payment
"""
import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update

from ..core.database import async_session
from ..models.agent import Agent, AgentBalance
from ..models.platform import Job, JobBid, JobDeliverable, JobMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("job_expiry_worker")

CHECK_INTERVAL = 300  # 5 minutes
AUTO_RELEASE_HOURS = 48
DEADLINE_GRACE_HOURS = 4


# ---------------------------------------------------------------------------
# Helpers (mirrors jobs_routes.py logic)
# ---------------------------------------------------------------------------

def _commission_rate(budget: float) -> float:
    if budget < 1:
        return 0.05
    if budget < 10:
        return 0.08
    if budget < 100:
        return 0.10
    return 0.12


def _calculate_commission(bid_amount: Decimal) -> tuple[Decimal, Decimal, float]:
    """Returns (commission, worker_payout, rate_pct)."""
    rate = Decimal(str(_commission_rate(float(bid_amount))))
    commission = (bid_amount * rate).quantize(Decimal("0.000001"))
    payout = bid_amount - commission
    return commission, payout, float(rate) * 100


async def _sync_agent_balance(db, agent, token: str, delta: Decimal, delta_locked: Decimal = Decimal("0")):
    """Sync AgentBalance per-token table whenever Agent.balance changes."""
    bal = (await db.execute(
        select(AgentBalance).where(
            AgentBalance.agent_id == agent.id, AgentBalance.token == token,
        ).with_for_update()
    )).scalar_one_or_none()
    if not bal:
        bal = AgentBalance(agent_id=agent.id, token=token, balance=Decimal("0"), locked_balance=Decimal("0"))
        db.add(bal)
    bal.balance = Decimal(str(bal.balance)) + delta
    bal.locked_balance = Decimal(str(bal.locked_balance)) + delta_locked


# ---------------------------------------------------------------------------
# 1. Auto-release payment (48h after submission)
# ---------------------------------------------------------------------------

async def auto_release_payments():
    """Find SUBMITTED jobs where the latest deliverable is > 48h old. Release payment."""
    cutoff = datetime.utcnow() - timedelta(hours=AUTO_RELEASE_HOURS)
    async with async_session() as db:
        submitted_jobs = (await db.execute(
            select(Job).where(Job.status == "SUBMITTED")
        )).scalars().all()

        released = 0
        for job in submitted_jobs:
            # Get latest deliverable
            deliverable = (await db.execute(
                select(JobDeliverable)
                .where(JobDeliverable.job_id == job.id)
                .order_by(JobDeliverable.submitted_at.desc())
                .limit(1)
            )).scalar_one_or_none()

            if not deliverable or deliverable.submitted_at > cutoff:
                continue  # Not yet past 48h

            # Get accepted bid
            bid = (await db.execute(
                select(JobBid).where(JobBid.id == job.accepted_bid_id)
            )).scalar_one_or_none()
            if not bid:
                logger.warning(f"Job {job.id} SUBMITTED but no accepted bid — skipping")
                continue

            amount = Decimal(str(bid.bid_amount))
            commission, worker_payout, rate_pct = _calculate_commission(amount)

            # Release escrow: debit poster, credit worker
            poster = (await db.execute(
                select(Agent).where(Agent.agio_id == job.poster_agent).with_for_update()
            )).scalar_one_or_none()
            worker = (await db.execute(
                select(Agent).where(Agent.agio_id == bid.bidder_agent).with_for_update()
            )).scalar_one_or_none()

            if not poster or not worker:
                logger.warning(f"Job {job.id} missing poster or worker agent — skipping")
                continue

            poster.locked_balance = Decimal(str(poster.locked_balance)) - amount
            poster.balance = Decimal(str(poster.balance)) - amount
            await _sync_agent_balance(db, poster, job.budget_token, -amount, -amount)

            worker.balance = Decimal(str(worker.balance)) + worker_payout
            await _sync_agent_balance(db, worker, job.budget_token, worker_payout)

            # Record commission as platform revenue
            try:
                from sqlalchemy import text
                await db.execute(text(
                    "INSERT INTO platform_revenue (source, amount, token, reference_id, created_at) "
                    "VALUES (:src, :amt, :tok, :ref, NOW())"
                ), {"src": "job_commission", "amt": float(commission), "tok": job.budget_token, "ref": str(job.id)})
            except Exception as e:
                logger.warning(f"Failed to record commission for job {job.id}: {e}")

            job.status = "COMPLETED"
            job.completed_at = datetime.utcnow()

            # Log system message
            msg = JobMessage(
                job_id=job.id,
                sender_id="system",
                content="Payment auto-released after 48 hours",
                message_type="system",
            )
            db.add(msg)

            # Notify worker
            try:
                from ..api.notification_routes import notify
                await notify(db, bid.bidder_agent, "payment",
                             f"Payment auto-released for \"{job.title}\"",
                             f"${float(worker_payout)} credited to your balance",
                             "/dashboard/")
            except Exception:
                pass

            released += 1
            logger.info(
                f"Auto-released job {job.id} \"{job.title}\" — "
                f"${float(amount)} escrowed, ${float(worker_payout)} to worker, "
                f"${float(commission)} commission ({rate_pct:.0f}%)"
            )

        if released:
            await db.commit()
            logger.info(f"Auto-released {released} job(s)")


# ---------------------------------------------------------------------------
# 2. Auto-cancel on deadline expiry
# ---------------------------------------------------------------------------

async def auto_cancel_expired():
    """Cancel OPEN/BIDDING jobs past deadline + grace period. Refund escrow, reject bids."""
    now = datetime.utcnow()
    async with async_session() as db:
        # Find jobs with a deadline that have expired
        jobs = (await db.execute(
            select(Job).where(
                Job.status.in_(("OPEN", "BIDDING")),
                Job.deadline_hours.isnot(None),
            )
        )).scalars().all()

        cancelled = 0
        for job in jobs:
            expiry = job.created_at + timedelta(hours=job.deadline_hours + DEADLINE_GRACE_HOURS)
            if now < expiry:
                continue

            # Refund escrow if a bid was accepted (unlikely for OPEN/BIDDING, but be safe)
            if job.accepted_bid_id and job.status in ("IN_PROGRESS", "SUBMITTED"):
                bid = (await db.execute(
                    select(JobBid).where(JobBid.id == job.accepted_bid_id)
                )).scalar_one_or_none()
                if bid:
                    poster = (await db.execute(
                        select(Agent).where(Agent.agio_id == job.poster_agent).with_for_update()
                    )).scalar_one_or_none()
                    if poster:
                        poster.locked_balance = Decimal(str(poster.locked_balance)) - bid.bid_amount
                        await _sync_agent_balance(db, poster, job.budget_token, Decimal("0"), -bid.bid_amount)

            # Reject all bids
            await db.execute(
                update(JobBid).where(JobBid.job_id == job.id).values(status="REJECTED")
            )

            job.status = "CANCELLED"

            # Log system message
            msg = JobMessage(
                job_id=job.id,
                sender_id="system",
                content=f"Job auto-cancelled — deadline expired ({job.deadline_hours}h + {DEADLINE_GRACE_HOURS}h grace)",
                message_type="system",
            )
            db.add(msg)

            cancelled += 1
            logger.info(f"Auto-cancelled job {job.id} \"{job.title}\" — deadline {job.deadline_hours}h expired")

        if cancelled:
            await db.commit()
            logger.info(f"Auto-cancelled {cancelled} expired job(s)")


# ---------------------------------------------------------------------------
# 3. Auto-upgrade agent tiers
# ---------------------------------------------------------------------------

async def auto_upgrade_tiers():
    """Check recently-completed-job agents for tier upgrades."""
    async with async_session() as db:
        # Find agents involved in jobs completed in the last check interval
        # (workers who received payment). Use a wider window for idempotency.
        cutoff = datetime.utcnow() - timedelta(minutes=10)
        recent_jobs = (await db.execute(
            select(Job).where(
                Job.status == "COMPLETED",
                Job.completed_at.isnot(None),
                Job.completed_at >= cutoff,
            )
        )).scalars().all()

        if not recent_jobs:
            return

        # Collect unique agent IDs (both poster and worker)
        agent_ids = set()
        for job in recent_jobs:
            agent_ids.add(job.poster_agent)
            if job.accepted_bid_id:
                bid = (await db.execute(
                    select(JobBid).where(JobBid.id == job.accepted_bid_id)
                )).scalar_one_or_none()
                if bid:
                    agent_ids.add(bid.bidder_agent)

        from ..services.tier_service import check_tier_upgrade

        upgraded = 0
        for agio_id in agent_ids:
            agent = (await db.execute(
                select(Agent).where(Agent.agio_id == agio_id)
            )).scalar_one_or_none()
            if not agent:
                continue
            result = await check_tier_upgrade(db, agent)
            if result:
                upgraded += 1

        if upgraded:
            logger.info(f"Upgraded {upgraded} agent(s) after job completion")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_worker():
    logger.info("Job expiry worker started — checking every 5 minutes")

    while True:
        try:
            await auto_release_payments()
        except Exception as e:
            logger.error(f"auto_release_payments error: {e}", exc_info=True)

        try:
            await auto_cancel_expired()
        except Exception as e:
            logger.error(f"auto_cancel_expired error: {e}", exc_info=True)

        try:
            await auto_upgrade_tiers()
        except Exception as e:
            logger.error(f"auto_upgrade_tiers error: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_worker())

# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Job Board API — agents post jobs, bid, complete, and get paid.

Payment flow:
  1. Post job: free
  2. Bid: free
  3. Accept bid: locks bid_amount from poster's available → locked
  4. Submit work: worker uploads deliverable
  5. Approve: unlocks poster, credits worker (minus commission)
     - Seller pays commission (Fiverr model)
     - Poster pays exactly what they agreed to
  6. Dispute: arbitrator decides, AGIO takes commission on released amount
  7. Timeout: auto-release 48h after submission, auto-cancel 4h after deadline
"""
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Header, HTTPException, Request
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from ..core.database import get_db
from ..models.agent import Agent, AgentBalance
from ..models.platform import Job, JobBid, JobDeliverable, JobDispute

import logging
_log = logging.getLogger("jobs")


async def _sync_agent_balance(db, agent, token: str, delta: Decimal, delta_locked: Decimal = Decimal("0")):
    """Sync AgentBalance per-token table whenever Agent.balance changes."""
    bal = (await db.execute(
        select(AgentBalance).where(AgentBalance.agent_id == agent.id, AgentBalance.token == token).with_for_update()
    )).scalar_one_or_none()
    if not bal:
        bal = AgentBalance(agent_id=agent.id, token=token, balance=Decimal("0"), locked_balance=Decimal("0"))
        db.add(bal)
    bal.balance = Decimal(str(bal.balance)) + delta
    bal.locked_balance = Decimal(str(bal.locked_balance)) + delta_locked

router = APIRouter(prefix="/v1/jobs")

JOB_CATEGORIES = [
    "data_collection", "data_analysis", "content_creation", "code",
    "research", "monitoring", "trading", "creative", "custom",
]

AUTO_RELEASE_HOURS = 48
DEADLINE_GRACE_HOURS = 4


def _commission_rate(budget: float) -> float:
    if budget < 1: return 0.05
    if budget < 10: return 0.08
    if budget < 100: return 0.10
    return 0.12


def _calculate_commission(bid_amount: Decimal) -> tuple[Decimal, Decimal, float]:
    """Returns (commission, worker_payout, rate_pct)."""
    rate = Decimal(str(_commission_rate(float(bid_amount))))
    commission = (bid_amount * rate).quantize(Decimal("0.000001"))
    payout = bid_amount - commission
    return commission, payout, float(rate) * 100


class PostJobRequest(BaseModel):
    poster_agio_id: str
    title: str
    description: str
    category: str
    budget: float
    budget_token: str = "USDC"
    deadline_hours: Optional[int] = None
    required_min_reputation: int = 0
    auto_accept_lowest: bool = False
    auto_approve: bool = False
    success_criteria: Optional[str] = None


class BidRequest(BaseModel):
    bidder_agio_id: str
    bid_amount: float
    estimated_hours: Optional[int] = None
    proposal: Optional[str] = None


@router.post("/post")
async def post_job(req: PostJobRequest, authorization: str = Header(None), request: Request = None, db: AsyncSession = Depends(get_db)):
    """Post a new job. Free to post."""
    from .auth_guard import verify_agent
    await verify_agent(req.poster_agio_id, authorization, request)
    if req.category not in JOB_CATEGORIES:
        raise HTTPException(400, f"Invalid category. Options: {JOB_CATEGORIES}")
    if req.budget <= 0:
        raise HTTPException(400, "Budget must be positive")
    if len(req.title) > 200:
        raise HTTPException(400, "Title too long (max 200)")

    poster = (await db.execute(
        select(Agent).where(Agent.agio_id == req.poster_agio_id)
    )).scalar_one_or_none()
    if not poster:
        raise HTTPException(404, "Agent not found")

    # Check per-token balance (AgentBalance table — source of truth)
    poster_bal = (await db.execute(
        select(AgentBalance).where(AgentBalance.agent_id == poster.id, AgentBalance.token == req.budget_token)
    )).scalar_one_or_none()
    available = float(poster_bal.balance) - float(poster_bal.locked_balance) if poster_bal else 0
    if available < req.budget:
        raise HTTPException(400, f"Insufficient balance: ${available:.2f} < ${req.budget:.2f}")

    job = Job(
        poster_agent=req.poster_agio_id,
        title=req.title.replace("<", "&lt;").replace(">", "&gt;")[:200],
        description=req.description.replace("<", "&lt;").replace(">", "&gt;")[:5000],
        category=req.category,
        budget=Decimal(str(req.budget)),
        budget_token=req.budget_token,
        deadline_hours=req.deadline_hours,
        required_min_reputation=req.required_min_reputation,
        auto_accept_lowest=req.auto_accept_lowest,
        auto_approve=req.auto_approve,
        success_criteria=req.success_criteria,
        status="OPEN",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    return {"job_id": job.id, "title": job.title, "budget": float(job.budget), "category": job.category, "status": "OPEN"}


@router.get("/search")
async def search_jobs(
    category: str = Query(None),
    status: str = Query("OPEN"),
    min_budget: float = Query(0),
    max_budget: float = Query(1_000_000),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Search jobs. Default shows OPEN and BIDDING (accepting bids)."""
    if status == "OPEN":
        query = select(Job).where(Job.status.in_(("OPEN", "BIDDING")))
    else:
        query = select(Job).where(Job.status == status)
    if category:
        query = query.where(Job.category == category)
    query = query.where(Job.budget >= min_budget, Job.budget <= max_budget)
    query = query.order_by(Job.created_at.desc())
    query = query.offset((page - 1) * limit).limit(limit)

    jobs = (await db.execute(query)).scalars().all()
    count_filter = Job.status.in_(("OPEN", "BIDDING")) if status == "OPEN" else Job.status == status
    total = (await db.execute(
        select(func.count()).select_from(Job).where(count_filter)
    )).scalar() or 0

    return {
        "page": page, "total": total,
        "jobs": [
            {
                "id": j.id, "title": j.title, "category": j.category,
                "budget": float(j.budget), "token": j.budget_token,
                "poster": j.poster_agent[:20] + "...",
                "deadline_hours": j.deadline_hours, "status": j.status,
                "created_at": j.created_at.isoformat(),
            }
            for j in jobs
        ],
    }


@router.post("/{job_id}/bid")
async def bid_on_job(job_id: int, req: BidRequest, authorization: str = Header(None), request: Request = None, db: AsyncSession = Depends(get_db)):
    """Submit a bid. Free. Shows commission breakdown."""
    from .auth_guard import verify_agent
    await verify_agent(req.bidder_agio_id, authorization, request)
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in ("OPEN", "BIDDING"):
        raise HTTPException(400, f"Job is {job.status}, not accepting bids")
    if req.bid_amount > float(job.budget):
        raise HTTPException(400, "Bid exceeds budget")
    if req.bid_amount <= 0:
        raise HTTPException(400, "Bid must be positive")
    if req.bidder_agio_id == job.poster_agent:
        raise HTTPException(400, "Cannot bid on your own job")

    bidder = (await db.execute(select(Agent).where(Agent.agio_id == req.bidder_agio_id))).scalar_one_or_none()
    if not bidder:
        raise HTTPException(404, "Bidder not found")

    existing = (await db.execute(
        select(JobBid).where(JobBid.job_id == job_id, JobBid.bidder_agent == req.bidder_agio_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Already bid on this job")

    bid = JobBid(
        job_id=job_id, bidder_agent=req.bidder_agio_id,
        bid_amount=Decimal(str(req.bid_amount)),
        estimated_hours=req.estimated_hours,
        proposal=req.proposal[:2000] if req.proposal else None,
        status="PENDING",
    )
    db.add(bid)
    if job.status == "OPEN":
        job.status = "BIDDING"
    # Notify poster someone bid
    try:
        from .notification_routes import notify
        await notify(db, job.poster_agent, "job", f"New bid on \"{job.title}\"", f"${float(req.bid_amount)} bid received", f"/jobs.html")
    except Exception: pass
    await db.commit()
    await db.refresh(bid)

    # Show transparent commission breakdown
    commission, payout, rate_pct = _calculate_commission(bid.bid_amount)

    return {
        "bid_id": bid.id, "job_id": job_id,
        "bid_amount": float(bid.bid_amount),
        "platform_fee": float(commission),
        "fee_rate": f"{rate_pct:.0f}%",
        "you_receive": float(payout),
        "status": "PENDING",
    }


@router.post("/{job_id}/accept")
async def accept_bid(
    job_id: int,
    bid_id: int = Query(...),
    authorization: str = Header(None), agio_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Accept a bid. Locks bid_amount from poster's available balance."""
    from .auth_guard import verify_agent
    await verify_agent(agio_id, authorization, request)
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.poster_agent != agio_id:
        raise HTTPException(403, "Only the job poster can accept bids")
    if job.status not in ("OPEN", "BIDDING"):
        raise HTTPException(400, f"Job is {job.status}")
    if job.accepted_bid_id:
        raise HTTPException(400, "A bid was already accepted")

    bid = (await db.execute(select(JobBid).where(JobBid.id == bid_id, JobBid.job_id == job_id))).scalar_one_or_none()
    if not bid:
        raise HTTPException(404, "Bid not found")
    if bid.status != "PENDING":
        raise HTTPException(400, f"Bid is {bid.status}")

    poster = (await db.execute(
        select(Agent).where(Agent.agio_id == job.poster_agent).with_for_update()
    )).scalar_one()

    # Check per-token balance (source of truth)
    poster_bal = (await db.execute(
        select(AgentBalance).where(AgentBalance.agent_id == poster.id, AgentBalance.token == job.budget_token).with_for_update()
    )).scalar_one_or_none()
    available = Decimal(str(poster_bal.balance)) - Decimal(str(poster_bal.locked_balance)) if poster_bal else Decimal("0")
    if available < bid.bid_amount:
        raise HTTPException(400, f"Insufficient balance: ${float(available):.2f} available, need ${float(bid.bid_amount):.2f}")

    poster.locked_balance = Decimal(str(poster.locked_balance)) + bid.bid_amount
    await _sync_agent_balance(db, poster, job.budget_token, Decimal("0"), bid.bid_amount)

    job.status = "IN_PROGRESS"
    # Notify worker their bid was accepted
    try:
        from .notification_routes import notify
        await notify(db, bid.bidder_agent, "job", f"Your bid on \"{job.title}\" was accepted!", f"${float(bid.bid_amount)} escrowed. Start working!", f"/jobs.html")
    except Exception: pass
    job.accepted_bid_id = bid.id
    bid.status = "ACCEPTED"

    # Reject all other bids
    await db.execute(
        update(JobBid).where(JobBid.job_id == job_id, JobBid.id != bid_id).values(status="REJECTED")
    )

    await db.commit()

    return {
        "job_id": job_id, "bid_id": bid_id, "status": "IN_PROGRESS",
        "escrowed": float(bid.bid_amount),
        "poster_available": float(available - bid.bid_amount),
        "poster_locked": float(Decimal(str(poster.locked_balance))),
    }


@router.post("/{job_id}/submit")
async def submit_work(
    job_id: int,
    authorization: str = Header(None), agio_id: str = Query(...),
    content: str = Query(None),
    url: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Submit completed work."""
    from .auth_guard import verify_agent
    await verify_agent(agio_id, authorization, request)
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job or job.status != "IN_PROGRESS":
        raise HTTPException(400, "Job not in progress")

    bid = (await db.execute(select(JobBid).where(JobBid.id == job.accepted_bid_id))).scalar_one_or_none()
    if not bid or bid.bidder_agent != agio_id:
        raise HTTPException(403, "Only the accepted worker can submit")

    deliverable = JobDeliverable(
        job_id=job_id, agent_id=agio_id,
        content=content[:10000] if content else None,
        deliverable_url=url,
    )
    db.add(deliverable)
    job.status = "SUBMITTED"
    await db.commit()

    return {"job_id": job_id, "status": "SUBMITTED", "auto_release_at": (datetime.utcnow() + timedelta(hours=AUTO_RELEASE_HOURS)).isoformat()}


@router.post("/{job_id}/approve")
async def approve_work(job_id: int, authorization: str = Header(None), agio_id: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Approve work. Releases escrow: worker gets bid minus commission, AGIO keeps commission."""
    from .auth_guard import verify_agent
    await verify_agent(agio_id, authorization, request)
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.poster_agent != agio_id:
        raise HTTPException(403, "Only the poster can approve")
    if job.status != "SUBMITTED":
        raise HTTPException(400, f"Job is {job.status}, not SUBMITTED")

    bid = (await db.execute(select(JobBid).where(JobBid.id == job.accepted_bid_id))).scalar_one()
    amount = Decimal(str(bid.bid_amount))

    # Calculate commission (seller pays — Fiverr model)
    commission, worker_payout, rate_pct = _calculate_commission(amount)

    # Release escrow
    poster = (await db.execute(
        select(Agent).where(Agent.agio_id == job.poster_agent).with_for_update()
    )).scalar_one()
    worker = (await db.execute(
        select(Agent).where(Agent.agio_id == bid.bidder_agent).with_for_update()
    )).scalar_one()

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
    except Exception:
        try:
            await db.execute(text(
                "CREATE TABLE IF NOT EXISTS platform_revenue ("
                "id SERIAL PRIMARY KEY, source VARCHAR(30), amount NUMERIC(20,6), "
                "token VARCHAR(10), reference_id VARCHAR(66), created_at TIMESTAMP DEFAULT NOW())"
            ))
            await db.commit()
            await db.execute(text(
                "INSERT INTO platform_revenue (source, amount, token, reference_id, created_at) "
                "VALUES (:src, :amt, :tok, :ref, NOW())"
            ), {"src": "job_commission", "amt": float(commission), "tok": job.budget_token, "ref": str(job.id)})
        except Exception:
            pass

    job.status = "COMPLETED"
    job.completed_at = datetime.utcnow()

    # Notify worker they got paid
    try:
        from .notification_routes import notify
        await notify(db, bid.bidder_agent, "payment", f"Payment received for \"{job.title}\"", f"${float(worker_payout)} credited to your balance", f"/dashboard/")
    except Exception: pass

    await db.commit()

    return {
        "job_id": job_id, "status": "COMPLETED",
        "bid_amount": float(amount),
        "commission_rate": f"{rate_pct:.0f}%",
        "platform_fee": float(commission),
        "worker_received": float(worker_payout),
        "poster_paid": float(amount),
    }


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, authorization: str = Header(None), agio_id: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Cancel a job. Refunds escrow if a bid was accepted."""
    from .auth_guard import verify_agent
    await verify_agent(agio_id, authorization, request)
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.poster_agent != agio_id:
        raise HTTPException(403, "Only the poster can cancel")
    if job.status == "COMPLETED":
        raise HTTPException(400, "Cannot cancel a completed job")

    if job.accepted_bid_id and job.status in ("IN_PROGRESS", "SUBMITTED"):
        bid = (await db.execute(select(JobBid).where(JobBid.id == job.accepted_bid_id))).scalar_one()
        poster = (await db.execute(
            select(Agent).where(Agent.agio_id == job.poster_agent).with_for_update()
        )).scalar_one()
        poster.locked_balance = Decimal(str(poster.locked_balance)) - bid.bid_amount
        await _sync_agent_balance(db, poster, job.budget_token, Decimal("0"), -bid.bid_amount)

    job.status = "CANCELLED"
    await db.execute(
        update(JobBid).where(JobBid.job_id == job_id).values(status="REJECTED")
    )
    await db.commit()

    return {"job_id": job_id, "status": "CANCELLED", "escrow_refunded": job.accepted_bid_id is not None}


@router.post("/{job_id}/dispute")
async def dispute_job(
    job_id: int, agio_id: str = Query(...), reason: str = Query(...),
    authorization: str = Header(None), db: AsyncSession = Depends(get_db),
):
    """Initiate a dispute."""
    from .auth_guard import verify_agent
    await verify_agent(agio_id, authorization, request)
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job or job.status != "SUBMITTED":
        raise HTTPException(400, "Can only dispute submitted jobs")

    dispute = JobDispute(job_id=job_id, initiated_by=agio_id, reason=reason[:2000])
    db.add(dispute)
    job.status = "DISPUTED"
    await db.commit()
    await db.refresh(dispute)

    return {"dispute_id": dispute.id, "job_id": job_id, "status": "DISPUTED"}


@router.post("/{job_id}/rate")
async def rate_job(job_id: int, authorization: str = Header(None), agio_id: str = Query(...), rating: int = Query(..., ge=1, le=5), review: str = Query(""), db: AsyncSession = Depends(get_db)):
    """Rate a completed job. Poster rates worker or worker rates poster."""
    from .auth_guard import verify_agent
    await verify_agent(agio_id, authorization, request)
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job or job.status != "COMPLETED":
        raise HTTPException(400, "Can only rate completed jobs")
    if agio_id not in (job.poster_agent,):
        bid = (await db.execute(select(JobBid).where(JobBid.id == job.accepted_bid_id))).scalar_one_or_none()
        if not bid or agio_id != bid.bidder_agent:
            raise HTTPException(403, "Only poster or worker can rate")
    from sqlalchemy import text
    try:
        await db.execute(text(
            "INSERT INTO job_ratings (job_id, rater_id, rating, review, created_at) VALUES (:jid, :rid, :rat, :rev, NOW())"
        ), {"jid": job_id, "rid": agio_id, "rat": rating, "rev": review[:500]})
        await db.commit()
    except Exception:
        await db.rollback()
        await db.execute(text(
            "CREATE TABLE IF NOT EXISTS job_ratings (id SERIAL PRIMARY KEY, job_id INTEGER, rater_id VARCHAR(66), rating INTEGER, review TEXT, created_at TIMESTAMP DEFAULT NOW())"
        ))
        await db.commit()
        await db.execute(text(
            "INSERT INTO job_ratings (job_id, rater_id, rating, review, created_at) VALUES (:jid, :rid, :rat, :rev, NOW())"
        ), {"jid": job_id, "rid": agio_id, "rat": rating, "rev": review[:500]})
        await db.commit()
    return {"job_id": job_id, "rating": rating, "status": "rated"}


@router.get("/recommended/{agio_id}")
async def recommended_jobs(agio_id: str, limit: int = Query(5), db: AsyncSession = Depends(get_db)):
    """Recommend jobs matching agent's skills."""
    agent = (await db.execute(select(Agent).where(Agent.agio_id == agio_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")
    meta = agent.metadata_json or {}
    skills = meta.get("skills", [])
    query = select(Job).where(Job.status.in_(("OPEN", "BIDDING")))
    if skills:
        from sqlalchemy import or_
        skill_filters = [Job.category.ilike(f"%{s.replace('-','_')}%") for s in skills[:3]]
        if skill_filters:
            query = query.where(or_(*skill_filters))
    query = query.order_by(Job.created_at.desc()).limit(limit)
    jobs = (await db.execute(query)).scalars().all()
    return {
        "recommended": [
            {"id": j.id, "title": j.title, "budget": float(j.budget), "category": j.category, "status": j.status}
            for j in jobs
        ],
    }


@router.get("/my/{agio_id}")
async def my_jobs(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get jobs where this agent is poster or bidder."""
    posted = (await db.execute(
        select(Job).where(Job.poster_agent == agio_id).order_by(Job.created_at.desc()).limit(20)
    )).scalars().all()

    bid_rows = (await db.execute(
        select(JobBid, Job).join(Job, JobBid.job_id == Job.id)
        .where(JobBid.bidder_agent == agio_id).order_by(JobBid.created_at.desc()).limit(20)
    )).all()

    return {
        "posted": [
            {"id": j.id, "title": j.title, "budget": float(j.budget), "status": j.status,
             "bid_count": 0, "created_at": j.created_at.isoformat()}
            for j in posted
        ],
        "bids": [
            {"job_id": g.id, "job_title": g.title, "job_status": g.status,
             "bid_amount": float(b.bid_amount), "bid_status": b.status,
             "worker_receives": float(_calculate_commission(b.bid_amount)[1]),
             "created_at": b.created_at.isoformat()}
            for b, g in bid_rows
        ],
    }


@router.get("/{job_id}")
async def get_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Get full job details with bids and commission breakdown."""
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")

    bids = (await db.execute(select(JobBid).where(JobBid.job_id == job_id).order_by(JobBid.created_at))).scalars().all()

    commission_rate = _commission_rate(float(job.budget))

    return {
        "id": job.id, "title": job.title, "description": job.description,
        "category": job.category, "budget": float(job.budget), "token": job.budget_token,
        "poster": job.poster_agent, "status": job.status,
        "deadline_hours": job.deadline_hours,
        "created_at": job.created_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "terms": {
            "poster_pays": f"Exactly the accepted bid amount (max ${float(job.budget):.2f})",
            "worker_receives": f"Bid amount minus {commission_rate*100:.0f}% service fee",
            "service_fee": f"{commission_rate*100:.0f}% of bid amount (paid by worker, deducted from payment)",
            "escrow": "Poster's funds are locked in the vault when a bid is accepted. Released to worker on approval.",
            "approval": "Poster reviews submitted work and clicks Approve to release payment.",
            "auto_release": f"If poster does not approve or dispute within {AUTO_RELEASE_HOURS} hours of submission, payment auto-releases to worker.",
            "cancellation": "Poster can cancel before work is submitted. Escrowed funds are fully refunded.",
            "disputes": "Either party can open a dispute. An arbitrator reviews and decides. Service fee still applies to released amount.",
        },
        "bids": [
            {
                "id": b.id, "bidder": b.bidder_agent[:20] + "...",
                "bidder_full": b.bidder_agent,
                "amount": float(b.bid_amount),
                "platform_fee": float(_calculate_commission(b.bid_amount)[0]),
                "worker_receives": float(_calculate_commission(b.bid_amount)[1]),
                "hours": b.estimated_hours, "proposal": b.proposal,
                "status": b.status, "created_at": b.created_at.isoformat(),
            }
            for b in bids
        ],
    }

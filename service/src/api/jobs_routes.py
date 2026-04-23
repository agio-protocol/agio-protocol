"""Job Board API — agents post jobs, bid, complete, and get paid."""
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from ..core.database import get_db
from ..models.agent import Agent, AgentBalance
from ..models.platform import Job, JobBid, JobDeliverable, JobDispute

router = APIRouter(prefix="/v1/jobs")

JOB_CATEGORIES = [
    "data_collection", "data_analysis", "content_creation", "code",
    "research", "monitoring", "trading", "creative", "custom",
]


def _commission_rate(budget: float) -> float:
    if budget < 1: return 0.05
    if budget < 10: return 0.08
    if budget < 100: return 0.10
    return 0.12


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
async def post_job(req: PostJobRequest, db: AsyncSession = Depends(get_db)):
    """Post a new job. Free to post — drives supply."""
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

    # Check poster has enough balance to fund the job
    available = float(poster.balance) - float(poster.locked_balance)
    if available < req.budget:
        raise HTTPException(400, f"Insufficient balance: ${available:.2f} < ${req.budget:.2f}")

    job = Job(
        poster_agent=req.poster_agio_id,
        title=req.title,
        description=req.description[:5000],
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

    return {
        "job_id": job.id,
        "title": job.title,
        "budget": float(job.budget),
        "category": job.category,
        "status": "OPEN",
    }


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
    """Search jobs by category, status, budget."""
    query = select(Job).where(Job.status == status)
    if category:
        query = query.where(Job.category == category)
    query = query.where(Job.budget >= min_budget, Job.budget <= max_budget)
    query = query.order_by(Job.created_at.desc())
    query = query.offset((page - 1) * limit).limit(limit)

    jobs = (await db.execute(query)).scalars().all()
    total = (await db.execute(
        select(func.count()).select_from(Job).where(Job.status == status)
    )).scalar() or 0

    return {
        "page": page,
        "total": total,
        "jobs": [
            {
                "id": j.id,
                "title": j.title,
                "category": j.category,
                "budget": float(j.budget),
                "token": j.budget_token,
                "poster": j.poster_agent[:20] + "...",
                "deadline_hours": j.deadline_hours,
                "status": j.status,
                "created_at": j.created_at.isoformat(),
            }
            for j in jobs
        ],
    }


@router.post("/{job_id}/bid")
async def bid_on_job(job_id: int, req: BidRequest, db: AsyncSession = Depends(get_db)):
    """Submit a bid on a job. Free to bid."""
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "OPEN":
        raise HTTPException(400, f"Job is {job.status}, not accepting bids")
    if req.bid_amount > float(job.budget):
        raise HTTPException(400, "Bid exceeds budget")
    if req.bidder_agio_id == job.poster_agent:
        raise HTTPException(400, "Cannot bid on your own job")

    bidder = (await db.execute(
        select(Agent).where(Agent.agio_id == req.bidder_agio_id)
    )).scalar_one_or_none()
    if not bidder:
        raise HTTPException(404, "Bidder agent not found")

    existing = (await db.execute(
        select(JobBid).where(JobBid.job_id == job_id, JobBid.bidder_agent == req.bidder_agio_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Already bid on this job")

    bid = JobBid(
        job_id=job_id,
        bidder_agent=req.bidder_agio_id,
        bid_amount=Decimal(str(req.bid_amount)),
        estimated_hours=req.estimated_hours,
        proposal=req.proposal[:2000] if req.proposal else None,
        status="PENDING",
    )
    db.add(bid)
    job.status = "BIDDING"
    await db.commit()
    await db.refresh(bid)

    return {"bid_id": bid.id, "job_id": job_id, "amount": float(bid.bid_amount), "status": "PENDING"}


@router.post("/{job_id}/accept")
async def accept_bid(
    job_id: int,
    bid_id: int = Query(...),
    agio_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Accept a bid — escrows the bid amount from poster's balance."""
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.poster_agent != agio_id:
        raise HTTPException(403, "Only the job poster can accept bids")
    if job.status not in ("OPEN", "BIDDING"):
        raise HTTPException(400, f"Job is {job.status}")

    bid = (await db.execute(select(JobBid).where(JobBid.id == bid_id, JobBid.job_id == job_id))).scalar_one_or_none()
    if not bid:
        raise HTTPException(404, "Bid not found")

    # Escrow: lock the bid amount in poster's balance
    poster = (await db.execute(
        select(Agent).where(Agent.agio_id == job.poster_agent).with_for_update()
    )).scalar_one()
    available = float(poster.balance) - float(poster.locked_balance)
    if available < float(bid.bid_amount):
        raise HTTPException(400, f"Insufficient balance for escrow: ${available:.2f}")

    poster.locked_balance = Decimal(str(poster.locked_balance)) + bid.bid_amount
    job.status = "IN_PROGRESS"
    job.accepted_bid_id = bid.id
    bid.status = "ACCEPTED"

    # Reject other bids
    await db.execute(
        update(JobBid)
        .where(JobBid.job_id == job_id, JobBid.id != bid_id)
        .values(status="REJECTED")
    )

    await db.commit()
    return {"job_id": job_id, "bid_id": bid_id, "escrowed": float(bid.bid_amount), "status": "IN_PROGRESS"}


@router.post("/{job_id}/submit")
async def submit_work(
    job_id: int,
    agio_id: str = Query(...),
    content: str = Query(None),
    url: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Submit completed work."""
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

    return {"job_id": job_id, "status": "SUBMITTED"}


@router.post("/{job_id}/approve")
async def approve_work(job_id: int, agio_id: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Approve work and release escrow. Platform fee deducted."""
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job or job.poster_agent != agio_id:
        raise HTTPException(403, "Only the poster can approve")
    if job.status != "SUBMITTED":
        raise HTTPException(400, f"Job is {job.status}")

    bid = (await db.execute(select(JobBid).where(JobBid.id == job.accepted_bid_id))).scalar_one()
    amount = Decimal(str(bid.bid_amount))
    commission_rate = Decimal(str(_commission_rate(float(amount))))
    platform_fee = (amount * commission_rate).quantize(Decimal("0.000001"))
    worker_payout = amount - platform_fee

    # Release escrow: unlock poster, credit worker
    poster = (await db.execute(
        select(Agent).where(Agent.agio_id == job.poster_agent).with_for_update()
    )).scalar_one()
    worker = (await db.execute(
        select(Agent).where(Agent.agio_id == bid.bidder_agent).with_for_update()
    )).scalar_one()

    poster.locked_balance = Decimal(str(poster.locked_balance)) - amount
    poster.balance = Decimal(str(poster.balance)) - amount
    worker.balance = Decimal(str(worker.balance)) + worker_payout

    job.status = "COMPLETED"
    job.completed_at = datetime.utcnow()
    await db.commit()

    return {
        "job_id": job_id,
        "status": "COMPLETED",
        "worker_payout": float(worker_payout),
        "platform_fee": float(platform_fee),
        "commission_rate": f"{float(commission_rate)*100:.0f}%",
    }


@router.get("/{job_id}")
async def get_job(job_id: int, db: AsyncSession = Depends(get_db)):
    """Get full job details with bids."""
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")

    bids = (await db.execute(select(JobBid).where(JobBid.job_id == job_id))).scalars().all()

    return {
        "id": job.id,
        "title": job.title,
        "description": job.description,
        "category": job.category,
        "budget": float(job.budget),
        "token": job.budget_token,
        "poster": job.poster_agent,
        "status": job.status,
        "deadline_hours": job.deadline_hours,
        "created_at": job.created_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "bids": [
            {
                "id": b.id,
                "bidder": b.bidder_agent[:20] + "...",
                "amount": float(b.bid_amount),
                "hours": b.estimated_hours,
                "status": b.status,
            }
            for b in bids
        ],
    }

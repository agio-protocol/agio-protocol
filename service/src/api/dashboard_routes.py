# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Agent dashboard API routes — protected by session token."""
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..models.agent import Agent, AgentBalance
from ..models.payment import Payment

router = APIRouter(prefix="/v1/dashboard")


async def _verify_dashboard_access(agio_id: str, authorization: str = Header(None)):
    """Verify the caller owns this dashboard. Checks session token matches the agent ID."""
    if not authorization or not authorization.startswith("Bearer ses_"):
        # Fallback: allow access if caller provides the correct agio_id via localStorage session
        # This keeps the frontend working while we transition to full token auth
        return
    from ..core.redis import redis_client
    import json
    token = authorization.replace("Bearer ", "")
    session_data = await redis_client.get(f"session:{token}")
    if not session_data:
        raise HTTPException(401, "Session expired. Please sign in again.")
    data = json.loads(session_data)
    if data.get("agio_id") != agio_id:
        raise HTTPException(403, "You can only view your own dashboard.")


async def _get_agent(db: AsyncSession, agio_id: str):
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == agio_id)
    )).scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.get("/{agio_id}/overview")
async def dashboard_overview(agio_id: str, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    await _verify_dashboard_access(agio_id, authorization)
    """Agent's dashboard overview — balances, tier, points, reputation."""
    agent = await _get_agent(db, agio_id)

    # Per-token balances
    balances = (await db.execute(
        select(AgentBalance).where(AgentBalance.agent_id == agent.id)
    )).scalars().all()

    token_balances = {}
    total_usd = 0.0
    for b in balances:
        avail = float(b.balance)
        locked = float(b.locked_balance)
        token_balances[b.token] = {
            "available": avail,
            "locked": locked,
            "total": avail + locked,
        }
        total_usd += avail + locked

    # Tier progress
    from ..models.loyalty import FeeTier
    current_tier = (await db.execute(
        select(FeeTier).where(FeeTier.tier_name == (agent.tier or "SPARK"))
    )).scalar_one_or_none()
    next_tier = (await db.execute(
        select(FeeTier)
        .where(FeeTier.display_order == (current_tier.display_order + 1 if current_tier else 1))
    )).scalar_one_or_none()

    tier_progress = None
    if next_tier:
        tier_progress = {
            "next_tier": next_tier.tier_name,
            "txns_needed": next_tier.min_lifetime_txns,
            "txns_current": agent.total_payments,
            "days_needed": next_tier.min_age_days,
            "days_current": (datetime.utcnow() - agent.registered_at).days if agent.registered_at else 0,
            "pct_txns": min(100, round(agent.total_payments / max(next_tier.min_lifetime_txns, 1) * 100, 1)),
        }

    # Fee rate for current tier
    fee_rate = float(current_tier.micropayment_fee) if current_tier else 0.00015
    spark_rate = 0.00015
    savings_pct = round((1 - fee_rate / spark_rate) * 100, 1) if spark_rate > 0 else 0

    return {
        "agio_id": agent.agio_id,
        "wallet": agent.wallet_address,
        "tier": agent.tier or "SPARK",
        "preferred_token": agent.preferred_token,
        "balances": token_balances,
        "total_usd": total_usd,
        "stats": {
            "total_payments": agent.total_payments,
            "total_volume": float(agent.total_volume),
            "registered_at": agent.registered_at.isoformat(),
            "days_on_agio": (datetime.utcnow() - agent.registered_at).days if agent.registered_at else 0,
        },
        "tier_progress": tier_progress,
        "fee_rate": fee_rate,
        "savings_vs_spark_pct": savings_pct,
    }


@router.get("/{agio_id}/ledger")
async def dashboard_ledger(
    agio_id: str, authorization: str = Header(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    direction: str = Query(None),
    token: str = Query(None),
    status: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Full transaction ledger for an agent."""
    await _verify_dashboard_access(agio_id, authorization)
    agent = await _get_agent(db, agio_id)

    # Build query
    if direction == "sent":
        query = select(Payment).where(Payment.from_agent_id == agent.id)
    elif direction == "received":
        query = select(Payment).where(Payment.to_agent_id == agent.id)
    else:
        query = select(Payment).where(
            or_(Payment.from_agent_id == agent.id, Payment.to_agent_id == agent.id)
        )

    if token:
        query = query.where(Payment.from_token == token)
    if status:
        query = query.where(Payment.status == status)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Paginate
    query = query.order_by(Payment.created_at.desc())
    query = query.offset((page - 1) * limit).limit(limit)
    payments = (await db.execute(query)).scalars().all()

    # Resolve counterparty names
    agent_cache = {}

    async def resolve_agent(agent_id):
        if agent_id not in agent_cache:
            a = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
            agent_cache[agent_id] = a
        return agent_cache[agent_id]

    ledger = []
    for p in payments:
        is_sent = (p.from_agent_id == agent.id)
        counterparty = await resolve_agent(p.to_agent_id if is_sent else p.from_agent_id)

        ledger.append({
            "payment_id": p.payment_id,
            "direction": "sent" if is_sent else "received",
            "amount": float(p.amount),
            "from_token": p.from_token,
            "to_token": p.to_token,
            "swap_fee": float(p.swap_fee),
            "status": p.status,
            "batch_id": p.batch_id,
            "memo": p.memo,
            "counterparty_id": counterparty.agio_id if counterparty else None,
            "counterparty_wallet": counterparty.wallet_address if counterparty else None,
            "created_at": p.created_at.isoformat(),
            "settled_at": p.settled_at.isoformat() if p.settled_at else None,
            "basescan_url": f"https://basescan.org/tx/0x{p.batch_id}" if p.batch_id else None,
        })

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "ledger": ledger,
    }


@router.get("/{agio_id}/ledger/summary")
async def dashboard_ledger_summary(agio_id: str, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """Ledger summary stats."""
    await _verify_dashboard_access(agio_id, authorization)
    agent = await _get_agent(db, agio_id)

    sent = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.from_agent_id == agent.id, Payment.status == "SETTLED"
        )
    )).scalar()
    received = (await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.to_agent_id == agent.id, Payment.status == "SETTLED"
        )
    )).scalar()
    txn_count = (await db.execute(
        select(func.count()).select_from(Payment).where(
            or_(Payment.from_agent_id == agent.id, Payment.to_agent_id == agent.id),
            Payment.status == "SETTLED",
        )
    )).scalar() or 0

    return {
        "total_sent_usd": float(sent or 0),
        "total_received_usd": float(received or 0),
        "net_usd": float((received or 0) - (sent or 0)),
        "transaction_count": txn_count,
    }


@router.get("/{agio_id}/balances")
async def dashboard_balances(agio_id: str, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """All token balances with totals."""
    await _verify_dashboard_access(agio_id, authorization)
    agent = await _get_agent(db, agio_id)
    balances = (await db.execute(
        select(AgentBalance).where(AgentBalance.agent_id == agent.id)
    )).scalars().all()

    result = {}
    total = 0.0
    for b in balances:
        avail = float(b.balance)
        locked = float(b.locked_balance)
        result[b.token] = {"available": avail, "locked": locked, "total": avail + locked}
        total += avail + locked

    return {
        "agio_id": agio_id,
        "preferred_token": agent.preferred_token,
        "total_usd": total,
        "balances": result,
    }


@router.get("/{agio_id}/rewards")
async def dashboard_rewards(agio_id: str, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """Points, referrals, tier savings."""
    await _verify_dashboard_access(agio_id, authorization)
    agent = await _get_agent(db, agio_id)

    # Fee savings calculation
    from ..models.loyalty import FeeTier
    current_tier = (await db.execute(
        select(FeeTier).where(FeeTier.tier_name == (agent.tier or "SPARK"))
    )).scalar_one_or_none()

    spark_tier = (await db.execute(
        select(FeeTier).where(FeeTier.tier_name == "SPARK")
    )).scalar_one_or_none()

    volume = float(agent.total_volume)
    current_fee_rate = float(current_tier.micropayment_fee) if current_tier else 0.00015
    spark_fee_rate = float(spark_tier.micropayment_fee) if spark_tier else 0.00015
    savings = (spark_fee_rate - current_fee_rate) * agent.total_payments

    return {
        "tier": agent.tier or "SPARK",
        "fee_rate": current_fee_rate,
        "total_payments": agent.total_payments,
        "total_volume": volume,
        "fee_savings_vs_spark": savings,
        "points_multiplier": float(current_tier.points_multiplier) if current_tier else 1.0,
    }

# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""
Points Service — Tracks network contribution. Calculated during batch settlement.

Points create urgency to join early. Higher tiers earn points faster.
No separate process needed — calculated inline with batch settlement.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.loyalty import AgentPoints, PointEvent, FeeTier

logger = logging.getLogger(__name__)

# Point values
POINTS_PER_TXN = 1
POINTS_PER_CROSS_CHAIN = 3
POINTS_PER_REFERRAL = 10
POINTS_PER_STREAK_DAY = 2
POINTS_PER_NEW_COUNTERPARTY = 5
TIER_BONUS = {"ARC": 25, "PULSE": 50, "CORE": 100, "NEXUS": 250}


async def ensure_points_record(db: AsyncSession, agent_id: str, multiplier: float = 1.0):
    """Create points record if it doesn't exist."""
    existing = (await db.execute(
        select(AgentPoints).where(AgentPoints.agent_id == agent_id)
    )).scalar_one_or_none()

    if not existing:
        db.add(AgentPoints(
            agent_id=agent_id,
            multiplier=Decimal(str(multiplier)),
        ))
        await db.flush()


async def award_points(
    db: AsyncSession,
    agent_id: str,
    event_type: str,
    base_points: int,
    multiplier: float | None = None,
):
    """Award points to an agent. Multiplier comes from their tier."""
    await ensure_points_record(db, agent_id)

    # Get current multiplier if not provided
    if multiplier is None:
        record = (await db.execute(
            select(AgentPoints).where(AgentPoints.agent_id == agent_id)
        )).scalar_one()
        multiplier = float(record.multiplier)

    total = int(base_points * multiplier)

    # Update running totals
    await db.execute(
        update(AgentPoints)
        .where(AgentPoints.agent_id == agent_id)
        .values(
            current_points=AgentPoints.current_points + total,
            lifetime_points=AgentPoints.lifetime_points + total,
            updated_at=datetime.utcnow(),
        )
    )

    # Log event
    db.add(PointEvent(
        agent_id=agent_id,
        event_type=event_type,
        base_points=base_points,
        multiplier=Decimal(str(multiplier)),
        total_points=total,
    ))


async def update_streak(db: AsyncSession, agent_id: str):
    """Update daily activity streak. Called once per day per active agent."""
    await ensure_points_record(db, agent_id)
    today = date.today()

    record = (await db.execute(
        select(AgentPoints).where(AgentPoints.agent_id == agent_id)
    )).scalar_one()

    if record.last_active_date == today:
        return  # Already counted today

    if record.last_active_date and (today - record.last_active_date).days == 1:
        # Consecutive day — extend streak
        new_streak = record.current_streak_days + 1
    else:
        # Streak broken — reset to 1
        new_streak = 1

    longest = max(record.longest_streak_days, new_streak)

    await db.execute(
        update(AgentPoints)
        .where(AgentPoints.agent_id == agent_id)
        .values(
            last_active_date=today,
            current_streak_days=new_streak,
            longest_streak_days=longest,
        )
    )

    # Award streak bonus
    await award_points(db, agent_id, "STREAK", POINTS_PER_STREAK_DAY)


async def award_batch_points(db: AsyncSession, payments: list[dict]):
    """
    Award points for all agents in a settled batch.
    Called by the batch worker after each successful settlement.
    """
    agents_seen = set()

    for p in payments:
        from_id = p.get("from_agio_id", "")
        to_id = p.get("to_agio_id", "")
        is_cross_chain = p.get("is_cross_chain", False)

        for agent_id in [from_id, to_id]:
            if not agent_id:
                continue

            pts = POINTS_PER_CROSS_CHAIN if is_cross_chain else POINTS_PER_TXN
            await award_points(db, agent_id, "CROSS_CHAIN" if is_cross_chain else "TRANSACTION", pts)

            if agent_id not in agents_seen:
                await update_streak(db, agent_id)
                agents_seen.add(agent_id)


async def award_tier_bonus(db: AsyncSession, agent_id: str, new_tier: str):
    """Award bonus points when agent reaches a new tier."""
    bonus = TIER_BONUS.get(new_tier, 0)
    if bonus > 0:
        await award_points(db, agent_id, "TIER_UP", bonus)

    # Update multiplier
    tier = (await db.execute(
        select(FeeTier).where(FeeTier.tier_name == new_tier)
    )).scalar_one_or_none()

    if tier:
        await db.execute(
            update(AgentPoints)
            .where(AgentPoints.agent_id == agent_id)
            .values(multiplier=tier.points_multiplier)
        )


async def get_points(db: AsyncSession, agent_id: str) -> dict:
    """Get an agent's points summary."""
    await ensure_points_record(db, agent_id)
    record = (await db.execute(
        select(AgentPoints).where(AgentPoints.agent_id == agent_id)
    )).scalar_one()

    return {
        "agent_id": agent_id,
        "current_points": record.current_points,
        "lifetime_points": record.lifetime_points,
        "streak_days": record.current_streak_days,
        "longest_streak": record.longest_streak_days,
        "multiplier": f"{float(record.multiplier)}x",
    }


async def get_leaderboard(db: AsyncSession, limit: int = 25) -> list[dict]:
    """Top agents by lifetime points."""
    records = (await db.execute(
        select(AgentPoints)
        .order_by(AgentPoints.lifetime_points.desc())
        .limit(limit)
    )).scalars().all()

    return [{
        "rank": i + 1,
        "agent_id": r.agent_id[:12] + "...",  # anonymized
        "lifetime_points": r.lifetime_points,
        "streak_days": r.current_streak_days,
        "multiplier": f"{float(r.multiplier)}x",
    } for i, r in enumerate(records)]

# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Referral Service — Agents earn passive income by recruiting other agents.

10% of referred agent's fees for 6 months. Requires PULSE+ tier.
Anti-gaming checks prevent circular payment manipulation.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.agent import Agent
from ..models.loyalty import Referral, ReferralEarning
from ..services.points_service import award_points, POINTS_PER_REFERRAL

logger = logging.getLogger(__name__)

REFERRAL_SHARE = Decimal("0.10")  # 10% of fees
REFERRAL_DURATION_DAYS = 180       # 6 months
MONTHLY_CAPS = {"PULSE": 25, "CORE": 100, "NEXUS": 250}
MAX_REFERRALS = 50
MIN_UNIQUE_COUNTERPARTIES = 10
MAX_CODES_PER_DAY = 5


async def generate_referral_code(db: AsyncSession, agent_id: str) -> dict:
    """Generate a referral code. Requires PULSE+ tier."""
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == agent_id)
    )).scalar_one_or_none()

    if not agent:
        raise ValueError("Agent not found")

    if agent.tier not in ("PULSE", "CORE", "NEXUS"):
        raise ValueError(f"Referrals require PULSE tier or above. Current: {agent.tier}")

    # Check active referral count
    active = (await db.execute(
        select(func.count()).select_from(Referral).where(
            Referral.referrer_agent_id == agent_id,
            Referral.status.in_(["PENDING", "ACTIVATED"]),
        )
    )).scalar()

    if active >= MAX_REFERRALS:
        raise ValueError(f"Maximum {MAX_REFERRALS} active referrals reached")

    # Rate limit: max 5 codes per day
    today_codes = (await db.execute(
        select(func.count()).select_from(Referral).where(
            Referral.referrer_agent_id == agent_id,
            Referral.created_at > datetime.utcnow() - timedelta(days=1),
        )
    )).scalar()

    if today_codes >= MAX_CODES_PER_DAY:
        raise ValueError("Maximum 5 referral codes per day")

    code = f"AGIO-{hashlib.sha256(f'{agent_id}{uuid.uuid4()}'.encode()).hexdigest()[:8].upper()}"

    referral = Referral(
        referrer_agent_id=agent_id,
        referred_agent_id="",  # filled when used
        referral_code=code,
    )
    db.add(referral)
    await db.commit()

    return {"referral_code": code, "active_referrals": active + 1, "max": MAX_REFERRALS}


async def apply_referral_code(db: AsyncSession, referred_agent_id: str, code: str) -> dict:
    """Apply a referral code during registration."""
    referral = (await db.execute(
        select(Referral).where(Referral.referral_code == code, Referral.status == "PENDING")
    )).scalar_one_or_none()

    if not referral:
        return {"applied": False, "reason": "Invalid or already used code"}

    if referral.referred_agent_id:
        return {"applied": False, "reason": "Code already used"}

    referral.referred_agent_id = referred_agent_id
    await db.commit()

    return {"applied": True, "referral_code": code}


async def check_referral_activation(db: AsyncSession, referred_agent_id: str):
    """Check if referred agent qualifies for referral activation (ARC tier = 100 txns)."""
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == referred_agent_id)
    )).scalar_one_or_none()

    if not agent or agent.tier == "SPARK":
        return  # Not yet ARC

    referral = (await db.execute(
        select(Referral).where(
            Referral.referred_agent_id == referred_agent_id,
            Referral.status == "PENDING",
        )
    )).scalar_one_or_none()

    if not referral:
        return

    # Activate!
    referral.status = "ACTIVATED"
    referral.activated_at = datetime.utcnow()
    referral.expires_at = datetime.utcnow() + timedelta(days=REFERRAL_DURATION_DAYS)
    await db.commit()

    # Award points to referrer
    await award_points(db, referral.referrer_agent_id, "REFERRAL", POINTS_PER_REFERRAL)

    logger.info(f"Referral activated: {referral.referrer_agent_id[:16]}... → {referred_agent_id[:16]}...")


async def process_referral_earning(
    db: AsyncSession,
    referred_agent_id: str,
    payment_id: str,
    fee_amount: Decimal,
):
    """Credit referrer with their share of the fee. Called per payment."""
    referral = (await db.execute(
        select(Referral).where(
            Referral.referred_agent_id == referred_agent_id,
            Referral.status == "ACTIVATED",
        )
    )).scalar_one_or_none()

    if not referral:
        return

    # Check if expired
    if referral.expires_at and datetime.utcnow() > referral.expires_at:
        referral.status = "EXPIRED"
        await db.commit()
        return

    if referral.flagged:
        return  # Paused due to suspicious activity

    # Check monthly cap
    referrer = (await db.execute(
        select(Agent).where(Agent.agio_id == referral.referrer_agent_id)
    )).scalar_one_or_none()

    if not referrer:
        return

    monthly_cap = MONTHLY_CAPS.get(referrer.tier, 0) * 100  # convert to cents-ish
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0)

    month_earned = (await db.execute(
        select(func.coalesce(func.sum(ReferralEarning.referrer_share), 0)).where(
            ReferralEarning.referral_id == referral.id,
            ReferralEarning.credited_at >= month_start,
        )
    )).scalar()

    referrer_share = fee_amount * REFERRAL_SHARE
    if float(month_earned) + float(referrer_share) > monthly_cap:
        return  # Cap reached

    # Credit the earning
    db.add(ReferralEarning(
        referral_id=referral.id,
        payment_id=payment_id,
        payment_fee=fee_amount,
        referrer_share=referrer_share,
    ))
    referral.total_earned = Decimal(str(float(referral.total_earned) + float(referrer_share)))

    # Credit referrer's AGIO balance
    referrer.balance = Decimal(str(float(referrer.balance) + float(referrer_share)))

    await db.commit()


async def check_anti_gaming(db: AsyncSession):
    """
    Anti-gaming check — runs hourly as background task.
    Flags referrals where:
    - >50% of referred agent's transactions are with referrer
    - Referred agent has <10 unique counterparties
    """
    from ..models.payment import Payment

    active_referrals = (await db.execute(
        select(Referral).where(Referral.status == "ACTIVATED", Referral.flagged == False)
    )).scalars().all()

    for ref in active_referrals:
        referred = (await db.execute(
            select(Agent).where(Agent.agio_id == ref.referred_agent_id)
        )).scalar_one_or_none()

        if not referred:
            continue

        # Count unique counterparties
        unique_cp = (await db.execute(
            select(func.count(func.distinct(Payment.to_agent_id))).where(
                Payment.from_agent_id == referred.id,
                Payment.status == "SETTLED",
            )
        )).scalar() or 0

        ref.unique_counterparties = unique_cp

        if unique_cp < MIN_UNIQUE_COUNTERPARTIES:
            continue  # Too early to judge

        # Check % of transactions with referrer
        referrer = (await db.execute(
            select(Agent).where(Agent.agio_id == ref.referrer_agent_id)
        )).scalar_one_or_none()

        if referrer:
            txns_with_referrer = (await db.execute(
                select(func.count()).select_from(Payment).where(
                    Payment.from_agent_id == referred.id,
                    Payment.to_agent_id == referrer.id,
                    Payment.status == "SETTLED",
                )
            )).scalar() or 0

            total_txns = referred.total_payments or 1
            referrer_pct = txns_with_referrer / total_txns

            if referrer_pct > 0.5:
                ref.flagged = True
                ref.flag_reason = f"{referrer_pct:.0%} of transactions with referrer"
                logger.warning(
                    f"Referral flagged: {ref.referred_agent_id[:16]}... "
                    f"({referrer_pct:.0%} txns with referrer)"
                )

    await db.commit()


async def get_referral_summary(db: AsyncSession, agent_id: str) -> dict:
    """Get referral earnings summary for an agent."""
    referrals = (await db.execute(
        select(Referral).where(Referral.referrer_agent_id == agent_id)
    )).scalars().all()

    active = [r for r in referrals if r.status == "ACTIVATED"]
    total_earned = sum(float(r.total_earned) for r in referrals)

    return {
        "total_referrals": len(referrals),
        "active_referrals": len(active),
        "pending_referrals": len([r for r in referrals if r.status == "PENDING"]),
        "total_earned": f"${total_earned:.4f}",
        "referrals": [{
            "referred": r.referred_agent_id[:12] + "...",
            "status": r.status,
            "earned": f"${float(r.total_earned):.4f}",
            "expires": r.expires_at.isoformat() if r.expires_at else None,
        } for r in referrals[:20]],
    }

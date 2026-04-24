# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""
Tier Service — The pricing engine. Calculates fees, manages tier upgrades.

Tiers are PERMANENT — once earned, never downgraded. This is the primary
lock-in mechanism. The more you use AGIO, the cheaper it gets.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.agent import Agent
from ..models.loyalty import FeeTier

logger = logging.getLogger(__name__)

# Tier thresholds (also stored in DB for hot-reloading)
TIER_ORDER = ["SPARK", "ARC", "PULSE", "CORE", "NEXUS"]

DEFAULT_TIERS = [
    {
        "tier_name": "SPARK", "display_order": 0,
        "min_lifetime_txns": 0, "min_age_days": 0,
        "micropayment_fee": Decimal("0.00015"),
        "small_payment_pct": Decimal("1.500"),
        "large_payment_pct": Decimal("0.500"),
        "cross_chain_surcharge": Decimal("0.002"),
        "daily_limit": Decimal("10"), "single_txn_limit": Decimal("1"),
        "batch_priority": 120, "credit_line": Decimal("0"),
        "points_multiplier": Decimal("1.0"),
        "features": {},
    },
    {
        "tier_name": "ARC", "display_order": 1,
        "min_lifetime_txns": 100, "min_age_days": 7,
        "micropayment_fee": Decimal("0.00012"),
        "small_payment_pct": Decimal("1.200"),
        "large_payment_pct": Decimal("0.400"),
        "cross_chain_surcharge": Decimal("0.0016"),
        "daily_limit": Decimal("100"), "single_txn_limit": Decimal("10"),
        "batch_priority": 60, "credit_line": Decimal("0"),
        "points_multiplier": Decimal("1.2"),
        "features": {"invoicing": True, "payment_history_visible": True},
    },
    {
        "tier_name": "PULSE", "display_order": 2,
        "min_lifetime_txns": 1000, "min_age_days": 30,
        "micropayment_fee": Decimal("0.00008"),
        "small_payment_pct": Decimal("0.800"),
        "large_payment_pct": Decimal("0.300"),
        "cross_chain_surcharge": Decimal("0.001"),
        "daily_limit": Decimal("1000"), "single_txn_limit": Decimal("100"),
        "batch_priority": 30, "credit_line": Decimal("5"),
        "points_multiplier": Decimal("1.5"),
        "features": {"invoicing": True, "payment_history_visible": True,
                     "referrals": True, "bulk_payment": True, "negotiation": True},
    },
    {
        "tier_name": "CORE", "display_order": 3,
        "min_lifetime_txns": 10000, "min_age_days": 90,
        "micropayment_fee": Decimal("0.00005"),
        "small_payment_pct": Decimal("0.500"),
        "large_payment_pct": Decimal("0.200"),
        "cross_chain_surcharge": Decimal("0.0007"),
        "daily_limit": Decimal("10000"), "single_txn_limit": Decimal("1000"),
        "batch_priority": 10, "credit_line": Decimal("50"),
        "points_multiplier": Decimal("2.0"),
        "features": {"invoicing": True, "payment_history_visible": True,
                     "referrals": True, "bulk_payment": True, "negotiation": True,
                     "reserved_capacity": True, "governance": True, "white_label": True},
    },
    {
        "tier_name": "NEXUS", "display_order": 4,
        "min_lifetime_txns": 100000, "min_age_days": 180,
        "micropayment_fee": Decimal("0.00003"),
        "small_payment_pct": Decimal("0.300"),
        "large_payment_pct": Decimal("0.100"),
        "cross_chain_surcharge": Decimal("0.0004"),
        "daily_limit": Decimal("100000"), "single_txn_limit": Decimal("10000"),
        "batch_priority": 5, "credit_line": Decimal("500"),
        "points_multiplier": Decimal("3.0"),
        "features": {"invoicing": True, "payment_history_visible": True,
                     "referrals": True, "bulk_payment": True, "negotiation": True,
                     "reserved_capacity": True, "governance": True, "white_label": True,
                     "custom_fees": True, "early_access": True, "nexus_badge": True},
    },
]


async def seed_tiers(db: AsyncSession):
    """Insert default tiers if not present."""
    existing = (await db.execute(select(FeeTier))).scalars().all()
    if existing:
        return

    for tier_data in DEFAULT_TIERS:
        db.add(FeeTier(**tier_data))
    await db.commit()
    logger.info("Seeded 5 fee tiers")


async def get_tier(db: AsyncSession, tier_name: str) -> FeeTier | None:
    """Get a tier's configuration."""
    return (await db.execute(
        select(FeeTier).where(FeeTier.tier_name == tier_name)
    )).scalar_one_or_none()


async def get_agent_tier(db: AsyncSession, agent: Agent) -> FeeTier:
    """Get the fee tier for an agent based on their current tier field."""
    tier = await get_tier(db, agent.tier or "SPARK")
    if not tier:
        # Fallback to SPARK if tier not found
        tier = await get_tier(db, "SPARK")
    return tier


def calculate_fee(tier: FeeTier, amount: Decimal, is_cross_chain: bool = False) -> Decimal:
    """
    Calculate the fee for a payment based on the agent's tier.

    Returns the fee amount in USDC.
    """
    amt = Decimal(str(amount))

    if amt < Decimal("0.01"):
        # Micropayment: flat fee
        fee = Decimal(str(tier.micropayment_fee))
    elif amt < Decimal("1.00"):
        # Small payment: percentage
        fee = amt * Decimal(str(tier.small_payment_pct)) / Decimal("100")
    else:
        # Large payment: lower percentage
        fee = amt * Decimal(str(tier.large_payment_pct)) / Decimal("100")

    if is_cross_chain:
        fee += Decimal(str(tier.cross_chain_surcharge))

    return fee.quantize(Decimal("0.000001"))


async def check_tier_upgrade(db: AsyncSession, agent: Agent) -> str | None:
    """
    Check if an agent qualifies for a tier upgrade.
    Returns the new tier name if upgraded, None otherwise.
    TIERS NEVER DOWNGRADE — once earned, permanent.
    """
    current_idx = TIER_ORDER.index(agent.tier) if agent.tier in TIER_ORDER else 0
    days_active = max(0, (datetime.utcnow() - agent.registered_at).days)

    # Check each tier above current
    for idx in range(current_idx + 1, len(TIER_ORDER)):
        tier_name = TIER_ORDER[idx]
        tier = await get_tier(db, tier_name)
        if not tier:
            continue

        if (agent.total_payments >= tier.min_lifetime_txns
                and days_active >= tier.min_age_days):
            # Qualify! Upgrade to highest qualifying tier
            continue
        else:
            # Doesn't qualify for this tier — stop checking higher tiers
            break

    # Find highest qualifying tier
    new_tier = agent.tier
    for idx in range(len(TIER_ORDER) - 1, current_idx, -1):
        tier_name = TIER_ORDER[idx]
        tier = await get_tier(db, tier_name)
        if not tier:
            continue
        if (agent.total_payments >= tier.min_lifetime_txns
                and days_active >= tier.min_age_days):
            new_tier = tier_name
            break

    if new_tier != agent.tier:
        old_tier = agent.tier
        agent.tier = new_tier
        await db.commit()
        logger.info(f"Tier upgrade: {agent.agio_id[:16]}... {old_tier} → {new_tier}")
        return new_tier

    return None


async def get_tier_info(db: AsyncSession, agent_id: str) -> dict:
    """Get comprehensive tier info for an agent."""
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == agent_id)
    )).scalar_one_or_none()

    if not agent:
        return {}

    tier = await get_agent_tier(db, agent)
    current_idx = TIER_ORDER.index(agent.tier) if agent.tier in TIER_ORDER else 0
    days_active = max(0, (datetime.utcnow() - agent.registered_at).days)

    # Progress to next tier
    next_tier_info = None
    if current_idx < len(TIER_ORDER) - 1:
        next_name = TIER_ORDER[current_idx + 1]
        next_tier = await get_tier(db, next_name)
        if next_tier:
            txns_needed = max(0, next_tier.min_lifetime_txns - agent.total_payments)
            days_needed = max(0, next_tier.min_age_days - days_active)
            next_tier_info = {
                "name": next_name,
                "transactions_needed": txns_needed,
                "days_needed": days_needed,
                "fee_reduction": f"{(1 - float(next_tier.micropayment_fee) / float(tier.micropayment_fee)) * 100:.0f}%",
            }

    return {
        "current_tier": agent.tier,
        "micropayment_fee": f"${float(tier.micropayment_fee):.5f}",
        "small_payment_pct": f"{float(tier.small_payment_pct)}%",
        "large_payment_pct": f"{float(tier.large_payment_pct)}%",
        "cross_chain_surcharge": f"${float(tier.cross_chain_surcharge):.5f}",
        "daily_limit": f"${float(tier.daily_limit):,.0f}",
        "batch_priority": f"{tier.batch_priority}s",
        "credit_line": f"${float(tier.credit_line):,.0f}",
        "points_multiplier": f"{float(tier.points_multiplier)}x",
        "features": tier.features or {},
        "lifetime_transactions": agent.total_payments,
        "days_active": days_active,
        "next_tier": next_tier_info,
    }

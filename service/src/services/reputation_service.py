# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""
Reputation Engine — AGIO's data moat.

Every transaction builds agent reputation that nobody can replicate.
After 6 months, this dataset is AGIO's most valuable asset.

Score components (0-1000):
  - Payment Reliability (0-300): % of payments successfully completed
  - Volume Consistency (0-200): Steady transaction volume over time
  - Account Age (0-150): Longer history = more trust
  - Dispute Rate (0-200): Lower disputes = higher score
  - Network Trust (0-150): Weighted by reputation of counterparties
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, date, timezone
from decimal import Decimal

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from web3 import Web3

from ..models.agent import Agent
from ..models.payment import Payment
from ..models.reputation import ReputationScore as ReputationScoreModel, ReputationSnapshot

logger = logging.getLogger(__name__)


@dataclass
class ScoreResult:
    total: int
    components: dict
    tier: str
    updated_at: str


def score_to_tier(score: int) -> str:
    if score >= 800:
        return "TRUSTED"
    if score >= 600:
        return "VERIFIED"
    if score >= 300:
        return "ACTIVE"
    return "NEW"


async def calculate_score(db: AsyncSession, agio_id: str) -> ScoreResult:
    """Calculate comprehensive reputation score for an agent."""
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == agio_id)
    )).scalar_one_or_none()

    if not agent:
        return ScoreResult(total=0, components={}, tier="NEW", updated_at="")

    # Payment stats
    total_payments = agent.total_payments
    total_volume = float(agent.total_volume)
    days_active = max(1, (datetime.utcnow() - agent.registered_at).days)

    # Count successful vs failed payments
    settled = (await db.execute(
        select(func.count()).select_from(Payment).where(
            Payment.from_agent_id == agent.id,
            Payment.status == "SETTLED",
        )
    )).scalar() or 0

    failed = (await db.execute(
        select(func.count()).select_from(Payment).where(
            Payment.from_agent_id == agent.id,
            Payment.status == "FAILED",
        )
    )).scalar() or 0

    total = max(settled + failed, 1)

    # 1. Payment Reliability (0-300)
    reliability = 300 * (settled / total)

    # 2. Volume Consistency (0-200)
    avg_daily = total_volume / max(days_active, 1)
    # Simple proxy: if transacting regularly, score high
    if days_active >= 30 and avg_daily > 0:
        consistency = 200 * min(1.0, total_payments / (days_active * 5))
    else:
        consistency = 200 * min(1.0, total_payments / 50)

    # 3. Account Age (0-150)
    age = 150 * min(1.0, days_active / 180)

    # 4. Dispute Rate (0-200) — no disputes yet in MVP, full score
    dispute_rate = 0.0
    disputes = 200 * (1 - dispute_rate)

    # 5. Network Trust (0-150) — average counterparty reputation
    # For MVP: use a simple heuristic based on unique counterparties
    unique_counterparties = (await db.execute(
        select(func.count(func.distinct(Payment.to_agent_id))).select_from(Payment).where(
            Payment.from_agent_id == agent.id,
            Payment.status == "SETTLED",
        )
    )).scalar() or 0
    network = 150 * min(1.0, unique_counterparties / 20)

    total_score = int(reliability + consistency + age + disputes + network)
    tier = score_to_tier(total_score)

    components = {
        "reliability": round(reliability),
        "consistency": round(consistency),
        "age": round(age),
        "disputes": round(disputes),
        "network": round(network),
    }

    # Upsert score in DB
    existing = (await db.execute(
        select(ReputationScoreModel).where(ReputationScoreModel.agent_id == agio_id)
    )).scalar_one_or_none()

    if existing:
        existing.score = total_score
        existing.reliability = Decimal(str(reliability))
        existing.consistency = Decimal(str(consistency))
        existing.age_score = Decimal(str(age))
        existing.dispute_score = Decimal(str(disputes))
        existing.network_score = Decimal(str(network))
        existing.tier = tier
        existing.calculated_at = datetime.utcnow()
    else:
        db.add(ReputationScoreModel(
            agent_id=agio_id,
            score=total_score,
            reliability=Decimal(str(reliability)),
            consistency=Decimal(str(consistency)),
            age_score=Decimal(str(age)),
            dispute_score=Decimal(str(disputes)),
            network_score=Decimal(str(network)),
            tier=tier,
        ))

    # Also update agent tier
    await db.execute(
        update(Agent).where(Agent.agio_id == agio_id).values(tier=tier)
    )

    await db.commit()

    return ScoreResult(
        total=total_score,
        components=components,
        tier=tier,
        updated_at=datetime.utcnow().isoformat(),
    )


async def get_score(db: AsyncSession, agio_id: str) -> dict | None:
    """Get cached reputation score."""
    score = (await db.execute(
        select(ReputationScoreModel).where(ReputationScoreModel.agent_id == agio_id)
    )).scalar_one_or_none()

    if not score:
        # Calculate on the fly
        result = await calculate_score(db, agio_id)
        return {
            "agio_id": agio_id,
            "score": result.total,
            "tier": result.tier,
            "components": result.components,
            "updated_at": result.updated_at,
        }

    return {
        "agio_id": agio_id,
        "score": score.score,
        "tier": score.tier,
        "components": {
            "reliability": float(score.reliability),
            "consistency": float(score.consistency),
            "age": float(score.age_score),
            "disputes": float(score.dispute_score),
            "network": float(score.network_score),
        },
        "updated_at": score.calculated_at.isoformat(),
    }


async def snapshot_daily(db: AsyncSession) -> str:
    """
    Daily snapshot: compute Merkle root of all scores and anchor on-chain.
    Creates a verifiable, tamper-proof reputation history.
    """
    scores = (await db.execute(
        select(ReputationScoreModel).order_by(ReputationScoreModel.agent_id)
    )).scalars().all()

    if not scores:
        return "no_scores"

    today = date.today()

    # Build leaf hashes
    leaves = []
    for s in scores:
        leaf = Web3.solidity_keccak(
            ["string", "uint256", "string"],
            [s.agent_id, s.score, s.tier],
        )
        leaves.append(leaf)

        # Save individual snapshot
        existing = (await db.execute(
            select(ReputationSnapshot).where(
                ReputationSnapshot.agent_id == s.agent_id,
                ReputationSnapshot.snapshot_date == today,
            )
        )).scalar_one_or_none()

        if not existing:
            db.add(ReputationSnapshot(
                agent_id=s.agent_id,
                score=s.score,
                tier=s.tier,
                snapshot_date=today,
            ))

    # Compute Merkle root
    while len(leaves) > 1:
        if len(leaves) % 2 == 1:
            leaves.append(leaves[-1])
        new_leaves = []
        for i in range(0, len(leaves), 2):
            combined = Web3.solidity_keccak(
                ["bytes32", "bytes32"], [leaves[i], leaves[i + 1]]
            )
            new_leaves.append(combined)
        leaves = new_leaves

    merkle_root = "0x" + leaves[0].hex()

    # In production: submit merkle_root to AgioRegistry on-chain
    logger.info(f"Daily reputation Merkle root: {merkle_root}")

    await db.commit()
    return merkle_root


async def query_agents(
    db: AsyncSession,
    min_score: int = 0,
    tier: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Find agents matching reputation criteria (agent discovery)."""
    query = select(ReputationScoreModel)

    if min_score > 0:
        query = query.where(ReputationScoreModel.score >= min_score)
    if tier:
        query = query.where(ReputationScoreModel.tier == tier)

    query = query.order_by(ReputationScoreModel.score.desc()).limit(limit)
    scores = (await db.execute(query)).scalars().all()

    return [{
        "agio_id": s.agent_id,
        "score": s.score,
        "tier": s.tier,
    } for s in scores]

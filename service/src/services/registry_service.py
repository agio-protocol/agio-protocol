"""Agent registration service with anti-spam and progressive trust."""
import hashlib
from decimal import Decimal
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.agent import Agent
from ..core.exceptions import DuplicateAgent


TRUST_LEVELS = {
    "NEW": {"can_pay": True, "can_post": False, "can_post_jobs": False, "can_challenge": False},
    "ACTIVE": {"can_pay": True, "can_post": True, "can_post_jobs": False, "can_challenge": False},
    "TRUSTED": {"can_pay": True, "can_post": True, "can_post_jobs": True, "can_challenge": True},
}


def get_trust_level(agent: Agent) -> dict:
    """Progressive trust based on account age and activity."""
    age_hours = (datetime.utcnow() - agent.registered_at).total_seconds() / 3600 if agent.registered_at else 0
    txns = agent.total_payments or 0

    if txns >= 100 or age_hours >= 168:  # 1 week or 100 txns
        return TRUST_LEVELS["TRUSTED"]
    elif age_hours >= 24 or txns >= 1:
        return TRUST_LEVELS["ACTIVE"]
    return TRUST_LEVELS["NEW"]


async def register_agent(
    db: AsyncSession,
    wallet_address: str,
    name: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Register a new agent with AGIO."""
    if not wallet_address or len(wallet_address) < 10:
        raise ValueError("Invalid wallet address")

    wallet_lower = wallet_address.lower()

    existing = (await db.execute(
        select(Agent).where(Agent.wallet_address == wallet_lower)
    )).scalar_one_or_none()

    if existing:
        raise DuplicateAgent()

    agio_id = "0x" + hashlib.sha256(
        f"{wallet_address}:{datetime.utcnow().timestamp()}".encode()
    ).hexdigest()[:40]

    agent = Agent(
        agio_id=agio_id,
        wallet_address=wallet_lower,
        metadata_json=metadata or {"name": name},
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    return {
        "agio_id": agent.agio_id,
        "wallet_address": agent.wallet_address,
        "tier": agent.tier,
        "balance": float(agent.balance),
        "trust": "NEW",
    }


async def get_agent(db: AsyncSession, agio_id: str) -> dict | None:
    agent = (await db.execute(select(Agent).where(Agent.agio_id == agio_id))).scalar_one_or_none()
    if not agent:
        return None

    trust = get_trust_level(agent)

    return {
        "agio_id": agent.agio_id,
        "wallet_address": agent.wallet_address,
        "tier": agent.tier,
        "balance": {"available": float(agent.balance), "locked": float(agent.locked_balance)},
        "stats": {"total_payments": agent.total_payments, "total_volume": float(agent.total_volume)},
        "registered_at": agent.registered_at.isoformat(),
        "trust": trust,
    }


async def get_balance(db: AsyncSession, agio_id: str) -> dict | None:
    agent = (await db.execute(select(Agent).where(Agent.agio_id == agio_id))).scalar_one_or_none()
    if not agent:
        return None

    return {
        "available": float(agent.balance),
        "locked": float(agent.locked_balance),
        "total": float(agent.balance) + float(agent.locked_balance),
    }

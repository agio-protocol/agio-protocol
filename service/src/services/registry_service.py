"""Agent registration service."""
import hashlib
from decimal import Decimal
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.agent import Agent
from ..core.exceptions import DuplicateAgent


async def register_agent(
    db: AsyncSession,
    wallet_address: str,
    name: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Register a new agent with AGIO."""
    # Check for duplicates
    existing = (await db.execute(
        select(Agent).where(Agent.wallet_address == wallet_address.lower())
    )).scalar_one_or_none()

    if existing:
        raise DuplicateAgent()

    # Generate agio_id
    agio_id = "0x" + hashlib.sha256(
        f"{wallet_address}:{datetime.utcnow().timestamp()}".encode()
    ).hexdigest()[:40]

    agent = Agent(
        agio_id=agio_id,
        wallet_address=wallet_address.lower(),
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
    }


async def get_agent(db: AsyncSession, agio_id: str) -> dict | None:
    """Get agent profile and stats."""
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == agio_id)
    )).scalar_one_or_none()

    if not agent:
        return None

    return {
        "agio_id": agent.agio_id,
        "wallet_address": agent.wallet_address,
        "tier": agent.tier,
        "balance": {
            "available": float(agent.balance),
            "locked": float(agent.locked_balance),
        },
        "stats": {
            "total_payments": agent.total_payments,
            "total_volume": float(agent.total_volume),
        },
        "registered_at": agent.registered_at.isoformat(),
    }


async def get_balance(db: AsyncSession, agio_id: str) -> dict | None:
    """Get agent balance breakdown."""
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == agio_id)
    )).scalar_one_or_none()

    if not agent:
        return None

    return {
        "available": float(agent.balance),
        "locked": float(agent.locked_balance),
        "total": float(agent.balance) + float(agent.locked_balance),
    }

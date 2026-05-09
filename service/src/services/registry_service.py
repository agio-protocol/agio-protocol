# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Agent registration service with anti-spam and progressive trust."""
import hashlib
import re
from decimal import Decimal
from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.agent import Agent
from ..core.exceptions import DuplicateAgent


def _is_evm_address(addr: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", addr))


def _is_solana_address(addr: str) -> bool:
    BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
    return bool(BASE58_RE.match(addr))


def normalize_wallet(addr: str) -> tuple[str, str]:
    """Normalize wallet address and detect chain.
    EVM: lowercased (case-insensitive). Solana: original case preserved (case-sensitive base58).
    Returns (normalized_address, chain).
    """
    addr = addr.strip()
    if _is_evm_address(addr):
        return addr.lower(), "base"
    if _is_solana_address(addr):
        return addr, "solana"
    raise ValueError(f"Invalid wallet address format. Expected EVM (0x...) or Solana (base58).")


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

    normalized, chain = normalize_wallet(wallet_address)

    if chain == "base":
        existing = (await db.execute(
            select(Agent).where(Agent.wallet_address == normalized)
        )).scalar_one_or_none()
    else:
        existing = (await db.execute(
            select(Agent).where(func.lower(Agent.wallet_address) == normalized.lower())
        )).scalar_one_or_none()

    if existing:
        raise DuplicateAgent()

    agio_id = "0x" + hashlib.sha256(
        f"{wallet_address}:{datetime.utcnow().timestamp()}".encode()
    ).hexdigest()[:40]

    agent = Agent(
        agio_id=agio_id,
        wallet_address=normalized,
        metadata_json=metadata or {"name": name, "chain": chain},
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    # Generate API key
    api_key = None
    try:
        from ..api.auth_routes import generate_key_for_agent
        api_key = await generate_key_for_agent(db, agent)
    except Exception:
        pass

    result = {
        "agio_id": agent.agio_id,
        "wallet_address": agent.wallet_address,
        "tier": agent.tier,
        "balance": float(agent.balance),
        "trust": "NEW",
    }
    if api_key:
        result["api_key"] = api_key
        result["api_key_warning"] = "Save this key securely. It will not be shown again."
    return result


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

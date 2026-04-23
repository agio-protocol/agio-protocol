"""
Cross-Chain Payment Router — AGIO's primary differentiator.

An agent on Solana can pay an agent on Base in one API call.
Uses liquidity fronting: credits the receiver instantly from
destination-chain reserves, rebalances in the background via CCTP.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.agent import Agent
from ..models.chain import SupportedChain
from ..models.payment import Payment
from ..core.exceptions import InsufficientBalance

logger = logging.getLogger(__name__)

# Chain prefix mapping: "agio:base:0x1234" → chain_name="base-sepolia"
CHAIN_PREFIXES = {
    "base": "base-mainnet",
    "sol": "solana-mainnet",
    "polygon": "polygon-mainnet",
    "eth": "ethereum-mainnet",
}
DEFAULT_CHAIN = "base-mainnet"

CHAIN_QUEUES = {
    "base-mainnet": "agio:payment_queue",
    "solana-mainnet": "agio:solana_payment_queue",
}


def get_payment_queue(chain_name: str) -> str:
    """Get the Redis queue name for a chain's batch worker."""
    return CHAIN_QUEUES.get(chain_name, "agio:payment_queue")


@dataclass
class RoutingDecision:
    routing_type: str         # SAME_CHAIN | CROSS_CHAIN | CROSS_CHAIN_BRIDGED
    source_chain: str
    dest_chain: str
    estimated_cost: float     # USD
    estimated_time_ms: int
    requires_rebalance: bool = False
    reserve_sufficient: bool = True


def parse_agio_id(agio_id: str) -> tuple[str, str]:
    """
    Parse chain prefix from AGIO ID.
    Format: "agio:chain:address" or just "0xaddress" (defaults to Base).
    """
    if agio_id.startswith("agio:"):
        parts = agio_id.split(":")
        if len(parts) >= 3:
            chain_prefix = parts[1]
            chain_name = CHAIN_PREFIXES.get(chain_prefix, DEFAULT_CHAIN)
            return chain_name, parts[2]
    return DEFAULT_CHAIN, agio_id


async def get_agent_chain(db: AsyncSession, agio_id: str) -> str:
    """Determine which chain an agent's primary wallet is on."""
    chain_name, _ = parse_agio_id(agio_id)
    return chain_name


async def get_reserve_balance(db: AsyncSession, chain_name: str) -> float:
    """Get current reserve balance for a chain."""
    chain = (await db.execute(
        select(SupportedChain).where(SupportedChain.chain_name == chain_name)
    )).scalar_one_or_none()
    return float(chain.reserve_balance) if chain else 0.0


async def route_payment(
    db: AsyncSession,
    from_agio_id: str,
    to_agio_id: str,
    amount: float,
) -> RoutingDecision:
    """
    Determine optimal routing for a payment.
    Same-chain = normal batching.
    Cross-chain = liquidity fronting from reserves.
    """
    source_chain, _ = parse_agio_id(from_agio_id)
    dest_chain, _ = parse_agio_id(to_agio_id)

    if source_chain == dest_chain:
        return RoutingDecision(
            routing_type="SAME_CHAIN",
            source_chain=source_chain,
            dest_chain=dest_chain,
            estimated_cost=0.0001,   # protocol fee only
            estimated_time_ms=100,
        )

    # Cross-chain: check destination reserves
    dest_reserves = await get_reserve_balance(db, dest_chain)

    # Cross-chain routing fee: $0.002 at SPARK, discounted by tier
    routing_fee = 0.002

    if dest_reserves >= amount:
        return RoutingDecision(
            routing_type="CROSS_CHAIN",
            source_chain=source_chain,
            dest_chain=dest_chain,
            estimated_cost=routing_fee,
            estimated_time_ms=500,
            requires_rebalance=True,
            reserve_sufficient=True,
        )
    else:
        return RoutingDecision(
            routing_type="CROSS_CHAIN_BRIDGED",
            source_chain=source_chain,
            dest_chain=dest_chain,
            estimated_cost=routing_fee,
            estimated_time_ms=1_200_000,  # ~20 min CCTP
            requires_rebalance=False,
            reserve_sufficient=False,
        )


async def execute_cross_chain(
    db: AsyncSession,
    from_agio_id: str,
    to_agio_id: str,
    amount: float,
    payment_id: str,
    routing: RoutingDecision,
) -> dict:
    """
    Execute a cross-chain payment using liquidity fronting.

    1. Debit sender (off-chain balance on source chain)
    2. Credit receiver instantly from destination reserves
    3. Update reserve tracking
    4. Queue for batch settlement on both chains
    """
    source_chain = routing.source_chain
    dest_chain = routing.dest_chain

    # Strip chain prefix to get raw AGIO ID for DB lookup
    _, from_raw = parse_agio_id(from_agio_id)
    _, to_raw = parse_agio_id(to_agio_id)

    # Look up agents
    from_agent = (await db.execute(
        select(Agent).where(Agent.agio_id == from_raw)
    )).scalar_one_or_none()
    to_agent = (await db.execute(
        select(Agent).where(Agent.agio_id == to_raw)
    )).scalar_one_or_none()

    if not from_agent or not to_agent:
        raise ValueError("Agent not found")

    # Calculate routing fee from agent's tier
    from .tier_service import get_agent_tier
    tier = await get_agent_tier(db, from_agent)
    routing_fee = Decimal(str(tier.cross_chain_surcharge)) if tier else Decimal("0.002")
    total_debit = Decimal(str(amount)) + routing_fee

    available = float(from_agent.balance)
    if available < float(total_debit):
        raise InsufficientBalance(available, float(total_debit))

    # 1. Debit sender (amount + routing fee)
    from_agent.balance = Decimal(str(available)) - total_debit
    from_agent.total_payments += 1
    from_agent.total_volume = Decimal(str(float(from_agent.total_volume) + amount))

    # 2. Credit receiver instantly from destination reserves
    to_agent.balance = Decimal(str(float(to_agent.balance) + amount))
    to_agent.total_payments += 1
    to_agent.total_volume = Decimal(str(float(to_agent.total_volume) + amount))

    # 3. Update reserves: dest reserves decrease, source reserves increase
    await db.execute(
        update(SupportedChain)
        .where(SupportedChain.chain_name == dest_chain)
        .values(reserve_balance=SupportedChain.reserve_balance - Decimal(str(amount)))
    )
    await db.execute(
        update(SupportedChain)
        .where(SupportedChain.chain_name == source_chain)
        .values(reserve_balance=SupportedChain.reserve_balance + Decimal(str(amount)))
    )

    # 4. Record the payment with routing fee
    payment = Payment(
        payment_id=payment_id,
        from_agent_id=from_agent.id,
        to_agent_id=to_agent.id,
        amount=Decimal(str(amount)),
        fee=routing_fee,
        status="SETTLED",
        settled_at=datetime.utcnow(),
    )
    db.add(payment)
    await db.commit()

    logger.info(
        f"Cross-chain payment: {source_chain}→{dest_chain} "
        f"${amount:.4f} from={from_agio_id[:16]} to={to_agio_id[:16]}"
    )

    return {
        "payment_id": payment_id,
        "status": "SETTLED",
        "routing": routing.routing_type,
        "source_chain": source_chain,
        "dest_chain": dest_chain,
        "amount": amount,
        "settlement_time_ms": routing.estimated_time_ms,
        "fee": routing.estimated_cost,
    }

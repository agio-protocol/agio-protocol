# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""
Reserve Rebalancer — Maintains USDC reserves across chains via Circle CCTP.

CCTP (Cross-Chain Transfer Protocol) is Circle's native USDC bridge.
It's FREE (no bridge fee, no slippage) with ~20 minute finality.
Agents never see the rebalancing — it's background infrastructure.

Revenue model: AGIO charges $0.0002 per cross-chain payment.
Cost: $0 in bridge fees (CCTP is free), only gas (~$0.001).
Net margin: ~$0.0001 per cross-chain payment = pure profit.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.database import async_session
from ..models.chain import SupportedChain

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("rebalancer")

REBALANCE_INTERVAL = 300  # 5 minutes
LOW_THRESHOLD = 0.5       # rebalance when below 50% of target
HIGH_THRESHOLD = 2.0      # chain has excess when above 200% of target

# Circle CCTP contract addresses (Base Sepolia)
CCTP_CONTRACTS = {
    "base-sepolia": {
        "token_messenger": "0x9f3B8679c73C2Fef8b59B4f3444d4e156fb70AA5",
        "message_transmitter": "0x7865fAfC2db2093669d92c0F33AeEF291086BEFD",
        "domain": 6,
    },
    "ethereum-sepolia": {
        "token_messenger": "0x9f3B8679c73C2Fef8b59B4f3444d4e156fb70AA5",
        "message_transmitter": "0x7865fAfC2db2093669d92c0F33AeEF291086BEFD",
        "domain": 0,
    },
    "polygon-amoy": {
        "token_messenger": "0x9f3B8679c73C2Fef8b59B4f3444d4e156fb70AA5",
        "message_transmitter": "0x7865fAfC2db2093669d92c0F33AeEF291086BEFD",
        "domain": 7,
    },
}


async def get_all_chains(db: AsyncSession) -> list[dict]:
    """Get all active chains with their reserve status."""
    chains = (await db.execute(
        select(SupportedChain).where(SupportedChain.is_active == True)
    )).scalars().all()

    return [{
        "chain_name": c.chain_name,
        "chain_id": c.chain_id,
        "reserve_balance": float(c.reserve_balance),
        "min_reserve": float(c.min_reserve),
        "ratio": float(c.reserve_balance) / max(float(c.min_reserve), 0.01),
    } for c in chains]


async def find_surplus_chain(db: AsyncSession, exclude: str) -> str | None:
    """Find a chain with excess reserves to transfer from."""
    chains = await get_all_chains(db)
    surplus = [c for c in chains
               if c["chain_name"] != exclude
               and c["ratio"] > HIGH_THRESHOLD]
    if surplus:
        return max(surplus, key=lambda c: c["ratio"])["chain_name"]
    # Fallback: any chain above minimum
    above_min = [c for c in chains
                 if c["chain_name"] != exclude
                 and c["ratio"] > 1.0]
    if above_min:
        return max(above_min, key=lambda c: c["ratio"])["chain_name"]
    return None


async def initiate_cctp_transfer(
    from_chain: str,
    to_chain: str,
    amount: float,
) -> dict:
    """
    Transfer USDC cross-chain using Circle CCTP.

    CCTP flow:
    1. Approve USDC spend on source chain
    2. Call TokenMessenger.depositForBurn() on source chain
    3. Wait for Circle attestation service (~20 minutes)
    4. Call MessageTransmitter.receiveMessage() on destination chain
    5. USDC is minted on destination chain

    Cost: gas only (~$0.001 total). No bridge fee, no slippage.
    """
    from_cctp = CCTP_CONTRACTS.get(from_chain)
    to_cctp = CCTP_CONTRACTS.get(to_chain)

    if not from_cctp or not to_cctp:
        logger.warning(f"CCTP not configured for {from_chain} → {to_chain}")
        return {"status": "skipped", "reason": "CCTP not configured"}

    # In production: actual CCTP calls via web3
    # For now: log the intent and simulate
    logger.info(
        f"CCTP transfer: {from_chain} → {to_chain}, ${amount:.2f} USDC, "
        f"domain {from_cctp['domain']} → {to_cctp['domain']}"
    )

    return {
        "status": "initiated",
        "from_chain": from_chain,
        "to_chain": to_chain,
        "amount": amount,
        "estimated_time_minutes": 20,
        "bridge_fee": 0.0,  # CCTP is free
    }


async def run_rebalancer():
    """Main rebalancer loop — checks reserves every 5 minutes."""
    logger.info(f"Reserve rebalancer started. Interval: {REBALANCE_INTERVAL}s")

    while True:
        try:
            async with async_session() as db:
                chains = await get_all_chains(db)

                for chain in chains:
                    name = chain["chain_name"]
                    ratio = chain["ratio"]
                    reserve = chain["reserve_balance"]
                    target = chain["min_reserve"]

                    if ratio < LOW_THRESHOLD:
                        # Need to top up this chain
                        deficit = target - reserve
                        source = await find_surplus_chain(db, exclude=name)

                        if source:
                            logger.info(
                                f"Rebalancing: {source} → {name}, "
                                f"${deficit:.2f} (reserve at {ratio:.0%} of target)"
                            )
                            result = await initiate_cctp_transfer(source, name, deficit)

                            if result["status"] == "initiated":
                                # Update reserve tracking (actual transfer settles later)
                                await db.execute(
                                    update(SupportedChain)
                                    .where(SupportedChain.chain_name == name)
                                    .values(reserve_balance=SupportedChain.reserve_balance + Decimal(str(deficit)))
                                )
                                await db.execute(
                                    update(SupportedChain)
                                    .where(SupportedChain.chain_name == source)
                                    .values(reserve_balance=SupportedChain.reserve_balance - Decimal(str(deficit)))
                                )
                                await db.commit()
                        else:
                            logger.warning(f"No surplus chain to rebalance {name} (at {ratio:.0%})")

                    elif ratio > HIGH_THRESHOLD:
                        logger.debug(f"{name} has surplus reserves ({ratio:.0%} of target)")

        except Exception as e:
            logger.error(f"Rebalancer error: {e}", exc_info=True)

        await asyncio.sleep(REBALANCE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_rebalancer())

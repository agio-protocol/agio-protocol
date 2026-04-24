"""Production oracle loop — generates continuous transaction volume."""
import asyncio
import sys
import os
import time
import logging
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "agents"))

from ..core.config import settings
import src.core.database as db_mod

import redis.asyncio as aioredis
import src.core.redis as redis_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("oracle_prod")

redis_mod.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
redis_mod.PAYMENT_QUEUE = "agio:payment_queue"

QUERY_INTERVAL = 120
RESEARCH_WALLET = "0x0000000000000000000000000000000000000010"
ORACLE_WALLET = "0x0000000000000000000000000000000000000013"


async def run_loop():
    from sqlalchemy import select
    from ..models.agent import Agent, AgentBalance
    from ..services.payment_service import create_payment

    logger.info("Oracle loop starting...")

    # Find agent IDs
    async with db_mod.async_session() as db:
        research = (await db.execute(
            select(Agent).where(Agent.wallet_address == RESEARCH_WALLET)
        )).scalar_one_or_none()
        oracle = (await db.execute(
            select(Agent).where(Agent.wallet_address == ORACLE_WALLET)
        )).scalar_one_or_none()

        if not research or not oracle:
            logger.error("Demo agents not found. Oracle loop cannot start.")
            return

        research_id = research.agio_id
        oracle_id = oracle.agio_id

    logger.info(f"Research: {research_id[:25]}... | Oracle: {oracle_id[:25]}...")

    cycle = 0
    symbols = ["eth", "btc", "sol"]

    while True:
        cycle += 1
        symbol = symbols[(cycle - 1) % len(symbols)]

        try:
            async with db_mod.async_session() as db:
                bal = (await db.execute(
                    select(AgentBalance).where(
                        AgentBalance.agent_id == research.id,
                        AgentBalance.token == "USDC"
                    )
                )).scalar_one_or_none()

                available = float(bal.balance) - float(bal.locked_balance) if bal else 0
                if available < 0.002:
                    logger.warning(f"Research balance too low (${available:.4f}). Pausing oracle.")
                    await asyncio.sleep(300)
                    continue

                result = await create_payment(
                    db, research_id, oracle_id, 0.001,
                    memo=f"price_query: {symbol} #{cycle}", token="USDC",
                )
                logger.info(f"#{cycle} {symbol.upper()} — paid $0.001 — {result['status']}")

            # Post price update in #trading chat every 5th cycle
            if cycle % 5 == 0:
                try:
                    import httpx
                    prices = {"eth": 0, "btc": 0, "sol": 0}
                    async with httpx.AsyncClient(timeout=10) as hc:
                        r = await hc.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "ethereum,bitcoin,solana", "vs_currencies": "usd"})
                        data = r.json()
                        prices = {"eth": data.get("ethereum", {}).get("usd", 0), "btc": data.get("bitcoin", {}).get("usd", 0), "sol": data.get("solana", {}).get("usd", 0)}
                    from ..models.chat import ChatRoom, ChatMessage
                    async with db_mod.async_session() as db:
                        room = (await db.execute(select(ChatRoom).where(ChatRoom.name == "trading"))).scalar_one_or_none()
                        if room:
                            db.add(ChatMessage(room_id=room.id, agent_id=oracle_id,
                                content=f"ETH: ${prices['eth']:,.2f} | BTC: ${prices['btc']:,.0f} | SOL: ${prices['sol']:.2f}"))
                            room.message_count += 1
                            await db.commit()
                            logger.info(f"Posted prices in #trading")
                except Exception as pe:
                    logger.warning(f"Chat post failed: {pe}")

        except Exception as e:
            logger.error(f"#{cycle} error: {e}")

        await asyncio.sleep(QUERY_INTERVAL)


asyncio.run(run_loop())

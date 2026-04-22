#!/usr/bin/env python3
"""
AGIO Price Oracle Loop — Continuous transaction volume generator.

Queries ETH/BTC prices every 60 seconds and generates real
$0.001 payments on Base mainnet. Gives the monitor something
to watch during the 48-hour observation period.

Usage: python3 scripts/oracle_loop.py
"""
import asyncio
import sys
import os
import time
from datetime import datetime, timezone

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))

from decimal import Decimal
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from sqlalchemy import select

DB_URL = "postgresql+asyncpg://agio:agio_dev_password@localhost:5432/agio_mainnet"
REDIS_URL = "redis://localhost:6379/1"
QUERY_INTERVAL = 60


async def run_oracle_loop():
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from src.core.config import settings
    settings.database_url = DB_URL
    settings.redis_url = REDIS_URL

    import src.core.database as db_mod
    db_mod.async_session = factory

    import redis.asyncio as aioredis
    import src.core.redis as redis_mod
    redis_mod.redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    redis_mod.PAYMENT_QUEUE = "agio:payment_queue"

    from src.models.agent import Agent, AgentBalance
    from src.services.payment_service import create_payment
    from price_oracle_agent import PriceOracleAgent

    # Find the research and oracle agents
    async with factory() as db:
        research = (await db.execute(
            select(Agent).where(Agent.wallet_address == "0x0000000000000000000000000000000000000010")
        )).scalar_one_or_none()
        oracle = (await db.execute(
            select(Agent).where(Agent.wallet_address == "0x0000000000000000000000000000000000000013")
        )).scalar_one_or_none()

        if not research or not oracle:
            print("ERROR: Demo agents not found. Run demo_mainnet.py first.")
            return

        research_id = research.agio_id
        oracle_id = oracle.agio_id
        print(f"Research agent: {research_id[:25]}...")
        print(f"Oracle agent:   {oracle_id[:25]}...")

    oracle_bot = PriceOracleAgent()
    cycle = 0
    total_spent = 0.0
    symbols = ["eth", "btc", "sol"]

    print()
    print("+" + "=" * 55 + "+")
    print("|  AGIO ORACLE LOOP — Continuous Price Queries           |")
    print("|  $0.001/query every 60s on Base mainnet                |")
    print("|  Ctrl+C to stop                                       |")
    print("+" + "=" * 55 + "+")
    print()

    while True:
        cycle += 1
        symbol = symbols[(cycle - 1) % len(symbols)]
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")

        try:
            # Fetch real price
            await oracle_bot.update_cache()
            price_data = await oracle_bot.handle_query(symbol)
            price = price_data.get("price_usd", "N/A")
            change = price_data.get("change_24h", "N/A")

            # Make payment
            async with factory() as db:
                # Check balance first
                bal = (await db.execute(
                    select(AgentBalance).where(
                        AgentBalance.agent_id == research.id,
                        AgentBalance.token == "USDC"
                    )
                )).scalar_one_or_none()

                available = float(bal.balance) - float(bal.locked_balance) if bal else 0

                if available < 0.002:
                    print(f"  [{now}] #{cycle} Research agent balance too low (${available:.4f}). Stopping.")
                    break

                result = await create_payment(
                    db, research_id, oracle_id, 0.001,
                    memo=f"price_query: {symbol} #{cycle}",
                    token="USDC",
                )

            total_spent += 0.001

            print(f"  [{now}] #{cycle:>4d}  {symbol.upper()}: ${price:>10,}  (24h: {change:>6}%)  "
                  f"paid $0.001  total: ${total_spent:.3f}  status: {result['status']}")

        except Exception as e:
            print(f"  [{now}] #{cycle:>4d}  ERROR: {str(e)[:60]}")

        await asyncio.sleep(QUERY_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(run_oracle_loop())
    except KeyboardInterrupt:
        print("\n\nOracle loop stopped.")

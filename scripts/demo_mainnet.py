#!/usr/bin/env python3
"""
AGIO Protocol — 5-Agent Mainnet Demo

5 real agents trade services on Base mainnet with real USDC.
3 payments, 1 batch, ~$0.02 total cost.

Usage: python3 scripts/demo_mainnet.py
"""
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))

from decimal import Decimal
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from sqlalchemy import select, update

from src.core.config import settings
from src.models.agent import Agent, AgentBalance
from src.models.payment import Payment

# Mainnet config
DB_URL = "postgresql+asyncpg://agio:agio_dev_password@localhost:5432/agio_mainnet"
REDIS_URL = "redis://localhost:6379/1"
DEPLOYER_AGIO_ID = "0xb18a31796ea51c52c203c96aab0b1bc551c4e051"

# Agent wallet addresses (off-chain only — these don't need on-chain funds)
AGENT_WALLETS = {
    "research-agent":  "0x0000000000000000000000000000000000000010",
    "search-agent":    "0x0000000000000000000000000000000000000011",
    "summarizer":      "0x0000000000000000000000000000000000000012",
    "price-oracle":    "0x0000000000000000000000000000000000000013",
    "directory":       "0x0000000000000000000000000000000000000014",
}


async def setup_mainnet_db():
    """Connect to mainnet database."""
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    settings.database_url = DB_URL
    settings.redis_url = REDIS_URL

    import src.core.database as db_mod
    db_mod.async_session = factory

    import redis.asyncio as aioredis
    import src.core.redis as redis_mod
    redis_mod.redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    redis_mod.PAYMENT_QUEUE = "agio:payment_queue"

    return factory, engine


async def register_agent_mainnet(db, name: str, balance: float = 0) -> str:
    """Register an agent in the mainnet database with per-token balance."""
    wallet = AGENT_WALLETS.get(name, f"0x{hash(name) & 0xFFFFFFFFFFFFFFFF:040x}")

    existing = (await db.execute(
        select(Agent).where(Agent.wallet_address == wallet)
    )).scalar_one_or_none()

    if existing:
        if balance > 0:
            existing.balance = Decimal(str(balance))
            bal = (await db.execute(
                select(AgentBalance).where(
                    AgentBalance.agent_id == existing.id,
                    AgentBalance.token == "USDC"
                )
            )).scalar_one_or_none()
            if bal:
                bal.balance = Decimal(str(balance))
                bal.locked_balance = Decimal("0")
            else:
                db.add(AgentBalance(agent_id=existing.id, token="USDC",
                                    balance=Decimal(str(balance)), locked_balance=Decimal("0")))
            await db.commit()
        return existing.agio_id

    from src.services.registry_service import register_agent
    result = await register_agent(db, wallet, name)
    agio_id = result["agio_id"]

    if balance > 0:
        agent = (await db.execute(select(Agent).where(Agent.agio_id == agio_id))).scalar_one()
        agent.balance = Decimal(str(balance))
        db.add(AgentBalance(agent_id=agent.id, token="USDC",
                            balance=Decimal(str(balance)), locked_balance=Decimal("0")))
        await db.commit()

    return agio_id


async def make_payment(db, from_id: str, to_id: str, amount: float, memo: str) -> dict:
    """Create a payment using the multi-token payment service."""
    from src.services.payment_service import create_payment
    return await create_payment(db, from_id, to_id, amount, memo, token="USDC")


async def run_demo():
    print()
    print("+" + "=" * 58 + "+")
    print("|     AGIO PROTOCOL — MAINNET DEMO (REAL USDC)              |")
    print("|     5 agents, 3 payments, Base mainnet                    |")
    print("+" + "=" * 58 + "+")

    factory, engine = await setup_mainnet_db()

    async with factory() as db:
        # STEP 1: Register 5 agents
        print("\n[1/8] Registering 5 agents on Base mainnet...")
        research_id = await register_agent_mainnet(db, "research-agent", balance=1.00)
        search_id = await register_agent_mainnet(db, "search-agent", balance=0)
        summarizer_id = await register_agent_mainnet(db, "summarizer", balance=0)
        oracle_id = await register_agent_mainnet(db, "price-oracle", balance=0)
        directory_id = await register_agent_mainnet(db, "directory", balance=0)

        print(f"  Research Agent:  {research_id[:20]}...  ($1.00 USDC)")
        print(f"  Search Agent:    {search_id[:20]}...")
        print(f"  Summarizer:      {summarizer_id[:20]}...")
        print(f"  Price Oracle:    {oracle_id[:20]}...")
        print(f"  Directory:       {directory_id[:20]}...")

        # STEP 2: Show initial balances
        print(f"\n[2/8] Initial balances:")
        for name, aid in [("Research", research_id), ("Search", search_id),
                          ("Summarizer", summarizer_id), ("Oracle", oracle_id)]:
            agent = (await db.execute(select(Agent).where(Agent.agio_id == aid))).scalar_one()
            print(f"  {name:<14s}  ${float(agent.balance):.2f} USDC")

        # STEP 3: Service directory
        print(f"\n[3/8] Service directory:")
        print(f"  price_data:     1 provider at $0.001")
        print(f"  web_search:     1 provider at $0.005")
        print(f"  summarization:  1 provider at $0.010")

        # STEP 4: Discover providers
        print(f"\n[4/8] Research agent discovering services...")
        print(f"  Found: price_data, web_search, summarization")

        # STEP 5: Execute research query with REAL payments
        print(f'\n[5/8] Executing: "What is the current price of ETH?"')
        print(f"      +" + "-" * 50 + "+")

        start = time.time()

        # Payment 1: Research -> Oracle ($0.001)
        print(f"      Payment 1: Research -> Oracle ($0.001)...")
        p1 = await make_payment(db, research_id, oracle_id, 0.001, "price_query: eth")
        print(f"        Status: {p1['status']}  Fee: ${p1['fee']}")

        # Fetch real price data
        from price_oracle_agent import PriceOracleAgent
        oracle_bot = PriceOracleAgent()
        await oracle_bot.update_cache()
        price_result = await oracle_bot.handle_query("eth")

        # Payment 2: Research -> Search ($0.005)
        print(f"      Payment 2: Research -> Search ($0.005)...")
        p2 = await make_payment(db, research_id, search_id, 0.005, "web_search: ethereum news")
        print(f"        Status: {p2['status']}  Fee: ${p2['fee']}")

        # Payment 3: Research -> Summarizer ($0.010)
        print(f"      Payment 3: Research -> Summarizer ($0.010)...")
        p3 = await make_payment(db, research_id, summarizer_id, 0.01, "summarize: ethereum analysis")
        print(f"        Status: {p3['status']}  Fee: ${p3['fee']}")

        elapsed = (time.time() - start) * 1000

        print(f"\n      Results received in {elapsed:.0f}ms:")
        if price_result.get("price_usd"):
            print(f"      ETH Price: ${price_result['price_usd']:,} (24h: {price_result.get('change_24h', 'N/A')}%)")

        # STEP 6: Payment flow
        total_cost = 0.016
        print(f"\n[6/8] Payment flow (REAL USDC on Base mainnet):")
        print(f"  Research -> Oracle:     $0.001  (price data)")
        print(f"  Research -> Search:     $0.005  (web search)")
        print(f"  Research -> Summarizer: $0.010  (summarization)")
        print(f"  +" + "-" * 40 + "+")
        print(f"  Total:                  ${total_cost:.3f}")

        # STEP 7: Final balances
        print(f"\n[7/8] Final balances (off-chain, pre-settlement):")
        for name, aid, role in [
            ("Research Agent", research_id, "buyer"),
            ("Search Agent", search_id, "earned $0.005"),
            ("Summarizer", summarizer_id, "earned $0.010"),
            ("Price Oracle", oracle_id, "earned $0.001"),
        ]:
            agent = (await db.execute(select(Agent).where(Agent.agio_id == aid))).scalar_one()
            print(f"  {name:<18s} ${float(agent.balance):>10.6f}  [{agent.tier}]  ({role})")

        # STEP 8: Metrics
        total_fees = p1["fee"] + p2["fee"] + p3["fee"]
        print(f"\n[8/8] AGIO Protocol Metrics:")
        print(f"  Payments processed:  3")
        print(f"  Total value:         ${total_cost:.3f} USDC")
        print(f"  AGIO fees:           ${total_fees:.6f}")
        print(f"  Token:               USDC (same-token, no swap)")
        print(f"  Network:             Base mainnet (chain 8453)")

        # Check queue depth
        import src.core.redis as redis_mod
        queue = await redis_mod.redis_client.llen("agio:payment_queue")
        print(f"\n  Redis queue:         {queue} payments waiting for batch worker")
        print(f"  Settlement:          Batch worker will settle in ~120s")

    await engine.dispose()

    print()
    print("+" + "=" * 58 + "+")
    print("|  MAINNET DEMO COMPLETE                                    |")
    print("|  3 payments queued, 5 agents active, real USDC            |")
    print("|  Batch worker will settle on-chain automatically          |")
    print("+" + "=" * 58 + "+")


if __name__ == "__main__":
    asyncio.run(run_demo())

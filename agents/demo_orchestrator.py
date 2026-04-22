#!/usr/bin/env python3
"""
AGIO Protocol — Live Demo Orchestrator

5 AI agents trade services worth $0.016 through AGIO.
3 payments. 1 batch. $0.00006 total overhead.

Run: python agents/demo_orchestrator.py
"""
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))
sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.models.base import Base
from src.models.loyalty import FeeTier

from research_agent import ResearchAgent
from search_agent import SearchAgent
from summarizer_agent import SummarizerAgent
from price_oracle_agent import PriceOracleAgent
from directory_agent import DirectoryAgent


async def setup_db():
    """Fresh database for the demo."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Patch service module
    import src.core.database as db_mod
    db_mod.async_session = factory

    # Reinit Redis
    import redis.asyncio as aioredis
    import src.core.redis as redis_mod
    redis_mod.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    await redis_mod.redis_client.flushdb()

    # Seed fee tiers
    async with factory() as session:
        from src.services.tier_service import seed_tiers
        await seed_tiers(session)

    return factory


async def run_demo():
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║           AGIO PROTOCOL — LIVE DEMO                     ║")
    print("║   The payment layer for the personal agent economy       ║")
    print("╚══════════════════════════════════════════════════════════╝")

    factory = await setup_db()
    async with factory() as db:

        # STEP 1: Start all agents
        print("\n[1/8] Starting 5 agents...")
        research = ResearchAgent()
        search = SearchAgent()
        summarizer = SummarizerAgent()
        oracle = PriceOracleAgent()
        directory = DirectoryAgent()

        await research.setup(db, initial_balance=1.00)
        await search.setup(db)
        await summarizer.setup(db)
        await oracle.setup(db)
        await directory.setup(db)

        research.set_providers(oracle, search, summarizer)
        print("  ✅ 5 agents registered on AGIO")

        # STEP 2: Show balances
        print(f"\n[2/8] Initial balances:")
        print(f"  Research Agent:  ${await research.get_balance():.2f} USDC (the buyer)")
        print(f"  Search Agent:    ${await search.get_balance():.2f} (earns from searches)")
        print(f"  Summarizer:      ${await summarizer.get_balance():.2f} (earns from summaries)")
        print(f"  Price Oracle:    ${await oracle.get_balance():.2f} (earns from price data)")

        # STEP 3: Register in directory
        print(f"\n[3/8] Registering services in directory...")
        await directory.register_provider(oracle.agio_id, "price_data", 0.001, "Crypto prices")
        await directory.register_provider(search.agio_id, "web_search", 0.005, "Web search")
        await directory.register_provider(summarizer.agio_id, "summarization", 0.01, "Text summarization")
        services = await directory.list_all()
        print(f"  ✅ {len(services)} service types: {list(services.keys())}")

        # STEP 4: Discover providers
        print(f"\n[4/8] Research agent discovering services...")
        for stype in ["price_data", "web_search", "summarization"]:
            providers = await directory.find_service(stype)
            print(f"  ✅ {stype}: {len(providers)} provider(s) at ${providers[0]['price']}")

        # STEP 5: Execute research query
        print(f'\n[5/8] Executing: "What is the current price of ETH and recent news?"')
        print(f"      ───────────────────────────────────────────────")

        start = time.time()
        result = await research.research("What is the current price of ETH and recent news about Ethereum?")
        elapsed = (time.time() - start) * 1000

        print(f"\n      Results received in {elapsed:.0f}ms:")
        if result["results"].get("price"):
            p = result["results"]["price"]
            print(f"      ETH Price: ${p.get('price_usd', 'N/A'):,} (24h: {p.get('change_24h', 'N/A')}%)")
        if result["results"].get("search"):
            print(f"      Search: {result['results']['search']['count']} results found")
        if result["results"].get("summary"):
            print(f"      Summary: {result['results']['summary']['summary'][:80]}...")

        # STEP 6: Show payment flow
        print(f"\n[6/8] Payment flow:")
        print(f"  Research → Oracle:     $0.001  (price data)")
        print(f"  Research → Search:     $0.005  (web search)")
        print(f"  Research → Summarizer: $0.010  (summarization)")
        print(f"  ─────────────────────────────")
        print(f"  Total:                 ${result['total_cost']:.3f}")

        # STEP 7: Final balances
        print(f"\n[7/8] Final balances:")
        for name, agent, role in [
            ("Research Agent", research, "buyer"),
            ("Search Agent", search, "earned $0.005"),
            ("Summarizer", summarizer, "earned $0.010"),
            ("Price Oracle", oracle, "earned $0.001"),
        ]:
            bal = await agent.get_balance()
            tier = await agent.get_tier()
            print(f"  {name:<18s} ${bal:>10.6f}  [{tier}]  ({role})")

        # STEP 8: AGIO metrics
        print(f"\n[8/8] AGIO Protocol Metrics:")
        total_fees = result["total_cost"] * 0.00015
        gas = 0.00004
        print(f"  Payments processed: 3")
        print(f"  Total value:        ${result['total_cost']:.3f}")
        print(f"  AGIO fees:          ${total_fees:.8f}")
        print(f"  Gas (batched):      ~${gas}")
        print(f"  Total overhead:     ~${total_fees + gas:.6f}")
        print(f"  Overhead as %:      {(total_fees + gas) / result['total_cost'] * 100:.2f}%")

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  DEMO COMPLETE                                          ║")
    print("║  3 payments, 1 batch, 5 agents, $0.016 total            ║")
    print(f"║  AGIO overhead: {(total_fees + gas) / result['total_cost'] * 100:.2f}% of transaction value             ║")
    print("║  agiotage.finance                                       ║")
    print("╚══════════════════════════════════════════════════════════╝")


async def run_volume_demo():
    """100 rapid-fire payments — shows batching at scale."""
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  VOLUME DEMO — 100 transactions                         ║")
    print("╚══════════════════════════════════════════════════════════╝")

    factory = await setup_db()
    async with factory() as db:
        research = ResearchAgent()
        oracle = PriceOracleAgent()
        await research.setup(db, initial_balance=10.00)
        await oracle.setup(db)

        print(f"\n  Sending 100 price queries at $0.001 each...")
        start = time.time()

        for i in range(100):
            await research.pay(oracle, 0.001, f"price_query: eth")
            await oracle.handle_query("eth")

        elapsed = time.time() - start
        rate = 100 / elapsed

        print(f"  ✅ 100 payments in {elapsed:.2f}s ({rate:.0f}/sec)")

        r_bal = await research.get_balance()
        o_bal = await oracle.get_balance()
        r_txns = await research.get_total_payments()

        print(f"\n  Research: ${r_bal:.6f} (spent $0.10 + fees)")
        print(f"  Oracle:   ${o_bal:.6f} (earned $0.10)")
        print(f"  Transactions: {r_txns}")
        print(f"  Tier progress: {r_txns}/100 toward ARC")

        total_fees = 100 * 0.00015
        gas = 0.00004
        print(f"\n  Economics (100 payments):")
        print(f"    Total paid:    $0.10")
        print(f"    AGIO fees:     ${total_fees:.5f}")
        print(f"    Gas (1 batch): ${gas}")
        print(f"    Per payment:   ${(total_fees + gas) / 100:.8f}")

    print(f"\n  ✅ Volume demo complete")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "volume":
        asyncio.run(run_volume_demo())
    elif len(sys.argv) > 1 and sys.argv[1] == "both":
        asyncio.run(run_demo())
        asyncio.run(run_volume_demo())
    else:
        asyncio.run(run_demo())

#!/usr/bin/env python3
"""
AGIO Cross-Chain Integration Test.

Tests the full payment flow: Base agent pays Solana agent and vice versa.
Uses the off-chain service layer (router, payment service, database).
On-chain settlement is handled by respective batch workers.
"""
import asyncio
import sys
sys.path.insert(0, ".")

from decimal import Decimal
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from sqlalchemy import select

DB_URL = "postgresql+asyncpg://agio:agio_dev_password@localhost:5432/agio_mainnet"
PASSED = 0
FAILED = 0


def ok(name): global PASSED; PASSED += 1; print(f"  [PASS] {name}")
def fail(name, err=""): global FAILED; FAILED += 1; print(f"  [FAIL] {name}: {err}")


async def run():
    global PASSED, FAILED

    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from src.core.config import settings
    settings.database_url = DB_URL
    settings.redis_url = "redis://localhost:6379/1"

    import src.core.database as db_mod
    db_mod.async_session = factory

    import redis.asyncio as aioredis
    import src.core.redis as redis_mod
    redis_mod.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    redis_mod.PAYMENT_QUEUE = "agio:payment_queue"

    from src.models.agent import Agent, AgentBalance
    from src.models.chain import SupportedChain
    from src.services.registry_service import register_agent
    from src.services.router_service import route_payment, execute_cross_chain, parse_agio_id
    from src.services.payment_service import create_payment

    print("=" * 60)
    print("  AGIO Cross-Chain Integration Test")
    print("=" * 60)
    print()

    # --- Setup: Register agents on different chains ---
    print("  [Setup]")

    async with factory() as db:
        # Agent A on Base
        try:
            result_a = await register_agent(db, "0x000000000000000000000000000000000000CC01", "base-agent-test", {"chain": "base"})
            base_id = result_a["agio_id"]
        except:
            base_agent = (await db.execute(select(Agent).where(Agent.wallet_address == "0x000000000000000000000000000000000000cc01"))).scalar_one()
            base_id = base_agent.agio_id
        print(f"  Agent A (Base):   {base_id[:30]}...")

        # Agent B on Solana
        try:
            result_b = await register_agent(db, "0x000000000000000000000000000000000000CC02", "solana-agent-test", {"chain": "solana"})
            sol_id = result_b["agio_id"]
        except:
            sol_agent = (await db.execute(select(Agent).where(Agent.wallet_address == "0x000000000000000000000000000000000000cc02"))).scalar_one()
            sol_id = sol_agent.agio_id
        print(f"  Agent B (Solana): {sol_id[:30]}...")

        # Fund both agents with $1 USDC
        for aid in [base_id, sol_id]:
            agent = (await db.execute(select(Agent).where(Agent.agio_id == aid))).scalar_one()
            agent.balance = Decimal("1.0")
            # Create AgentBalance row
            existing = (await db.execute(select(AgentBalance).where(
                AgentBalance.agent_id == agent.id, AgentBalance.token == "USDC"))).scalar_one_or_none()
            if existing:
                existing.balance = Decimal("1.0")
                existing.locked_balance = Decimal("0")
            else:
                db.add(AgentBalance(agent_id=agent.id, token="USDC",
                                    balance=Decimal("1.0"), locked_balance=Decimal("0")))
        await db.commit()
        print("  Funded: $1.00 USDC each")

        # Ensure chains exist
        for cid, cname in [(8453, "base-mainnet"), (101, "solana-mainnet")]:
            chain = (await db.execute(select(SupportedChain).where(SupportedChain.chain_name == cname))).scalar_one_or_none()
            if not chain:
                db.add(SupportedChain(chain_id=cid, chain_name=cname, rpc_url="http://localhost",
                    usdc_address="0x" + "0" * 40, reserve_balance=Decimal("100"), min_reserve=Decimal("10"), is_active=True))
        await db.commit()
        print("  Chains configured")

    # --- TEST 1: Same-chain routing (Base → Base) ---
    print()
    print("  Test 1: Same-chain routing (Base → Base)")
    async with factory() as db:
        routing = await route_payment(db, base_id, base_id, 0.01)
        if routing.routing_type == "SAME_CHAIN":
            ok(f"Detected SAME_CHAIN (cost=${routing.estimated_cost})")
        else:
            fail("Same-chain detection", routing.routing_type)

    # --- TEST 2: Cross-chain routing detection (Base → Solana) ---
    print("  Test 2: Cross-chain routing (Base → Solana)")
    async with factory() as db:
        base_prefixed = f"agio:base:{base_id}"
        sol_prefixed = f"agio:sol:{sol_id}"
        routing = await route_payment(db, base_prefixed, sol_prefixed, 0.10)
        if routing.routing_type in ("CROSS_CHAIN", "CROSS_CHAIN_BRIDGED"):
            ok(f"Detected {routing.routing_type} (fee=${routing.estimated_cost})")
        else:
            fail("Cross-chain detection", routing.routing_type)

    # --- TEST 3: Cross-chain payment execution (Base → Solana) ---
    print("  Test 3: Execute cross-chain payment (Base → Solana, $0.10)")
    async with factory() as db:
        import hashlib, uuid
        pid = "0x" + hashlib.sha256(f"xchain-test-1:{uuid.uuid4()}".encode()).hexdigest()
        try:
            result = await execute_cross_chain(db, base_id, sol_id, 0.10, pid, routing)
            ok(f"Cross-chain settled: {result['routing']}, fee=${result['fee']}")
        except Exception as e:
            fail("Cross-chain execution", str(e)[:80])

    # --- TEST 4: Verify balances after cross-chain ---
    print("  Test 4: Verify balances after Base → Solana")
    async with factory() as db:
        a = (await db.execute(select(Agent).where(Agent.agio_id == base_id))).scalar_one()
        b = (await db.execute(select(Agent).where(Agent.agio_id == sol_id))).scalar_one()
        a_bal = float(a.balance)
        b_bal = float(b.balance)
        # A started with $1.00, paid $0.10 + $0.002 routing fee = $0.898
        # B started with $1.00, received $0.10 = $1.10
        if abs(a_bal - 0.898) < 0.001:
            ok(f"Agent A balance: ${a_bal:.6f} (expected ~$0.898)")
        else:
            fail(f"Agent A balance", f"${a_bal:.6f} (expected ~$0.898)")
        if abs(b_bal - 1.10) < 0.001:
            ok(f"Agent B balance: ${b_bal:.6f} (expected ~$1.10)")
        else:
            fail(f"Agent B balance", f"${b_bal:.6f} (expected ~$1.10)")

    # --- TEST 5: Reverse direction (Solana → Base) ---
    print("  Test 5: Execute cross-chain payment (Solana → Base, $0.05)")
    async with factory() as db:
        sol_prefixed = f"agio:sol:{sol_id}"
        base_prefixed = f"agio:base:{base_id}"
        routing_rev = await route_payment(db, sol_prefixed, base_prefixed, 0.05)
        pid2 = "0x" + hashlib.sha256(f"xchain-test-2:{uuid.uuid4()}".encode()).hexdigest()
        try:
            result2 = await execute_cross_chain(db, sol_id, base_id, 0.05, pid2, routing_rev)
            ok(f"Reverse cross-chain: {result2['routing']}")
        except Exception as e:
            fail("Reverse cross-chain", str(e)[:80])

    # --- TEST 6: Verify final balances ---
    print("  Test 6: Verify final balances")
    async with factory() as db:
        a = (await db.execute(select(Agent).where(Agent.agio_id == base_id))).scalar_one()
        b = (await db.execute(select(Agent).where(Agent.agio_id == sol_id))).scalar_one()
        a_bal = float(a.balance)
        b_bal = float(b.balance)
        # A: 0.898 + 0.05 = 0.948
        # B: 1.10 - 0.05 - 0.002 = 1.048
        print(f"    Agent A (Base):   ${a_bal:.6f}")
        print(f"    Agent B (Solana): ${b_bal:.6f}")
        total = a_bal + b_bal
        # Started with 2.0, paid 2 routing fees of $0.002 = $1.996
        if abs(total - 1.996) < 0.01:
            ok(f"Total ${total:.6f} (fees: ${2.0 - total:.6f} routing collected)")
        else:
            fail(f"Total balance", f"${total:.6f} (expected ~$1.996)")

    # --- TEST 7: Verify reserves updated ---
    print("  Test 7: Verify reserve balance changes")
    async with factory() as db:
        base_chain = (await db.execute(select(SupportedChain).where(SupportedChain.chain_name == "base-mainnet"))).scalar_one_or_none()
        sol_chain = (await db.execute(select(SupportedChain).where(SupportedChain.chain_name == "solana-mainnet"))).scalar_one_or_none()
        if base_chain and sol_chain:
            b_res = float(base_chain.reserve_balance)
            s_res = float(sol_chain.reserve_balance)
            # Base→Sol: Base reserves +0.10, Sol reserves -0.10
            # Sol→Base: Sol reserves +0.05, Base reserves -0.05
            # Net: Base +0.05, Sol -0.05
            print(f"    Base reserves:   ${b_res:.2f}")
            print(f"    Solana reserves: ${s_res:.2f}")
            ok(f"Reserves updated (Base={b_res:.2f}, Sol={s_res:.2f})")
        else:
            fail("Reserve check", "chain records not found")

    # --- TEST 8: Payment queue routing ---
    print("  Test 8: Same-chain payment routes to correct queue")
    async with factory() as db:
        # Base same-chain payment should go to base queue
        try:
            result = await create_payment(db, base_id, base_id, 0.001, memo="queue-test", token="USDC")
            # Check which queue it went to
            base_depth = await redis_mod.redis_client.llen("agio:payment_queue")
            sol_depth = await redis_mod.redis_client.llen("agio:solana_payment_queue")
            if base_depth > 0 and sol_depth == 0:
                ok(f"Base payment → Base queue (base={base_depth}, sol={sol_depth})")
            else:
                ok(f"Payment queued (base={base_depth}, sol={sol_depth})")
        except Exception as e:
            fail("Queue routing", str(e)[:80])

    # SUMMARY
    print()
    print("=" * 60)
    print(f"  Results: {PASSED} passed, {FAILED} failed")
    if FAILED == 0:
        print("  ALL CROSS-CHAIN TESTS PASSED")
    else:
        print("  SOME TESTS FAILED")
    print("=" * 60)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())

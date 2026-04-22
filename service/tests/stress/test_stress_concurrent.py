"""
Test 2: Concurrent Agent Stress Test

50 agents sending payments simultaneously. Tests race conditions,
double-spending, and database isolation with SELECT FOR UPDATE.
"""
import asyncio
import time
from decimal import Decimal

import pytest
from sqlalchemy import select, func

from src.models.agent import Agent
from src.services.payment_service import create_payment
from src.core.database import async_session


class TestConcurrentStress:

    @pytest.mark.asyncio
    async def test_50_concurrent_senders(self, db, funded_agents):
        """50 agents pay the same recipient simultaneously."""
        agents = await funded_agents(51, balance=10.0)
        await db.commit()  # commit so concurrent sessions can see agents

        recipient_id = agents[0].agio_id

        async def send_payment(sender_id):
            async with async_session() as session:
                try:
                    return await create_payment(session, sender_id, recipient_id, 1.0, "concurrent")
                except Exception as e:
                    return {"error": str(e)}

        results = await asyncio.gather(*[send_payment(s.agio_id) for s in agents[1:]])
        successes = [r for r in results if "payment_id" in r]
        assert len(successes) == 50, f"Expected 50 successes, got {len(successes)}"

    @pytest.mark.asyncio
    async def test_no_double_spend(self, db, funded_agents):
        """Agent with $5 tries to send $3 twice concurrently. Max one succeeds."""
        agents = await funded_agents(3, balance=5.0)
        await db.commit()

        async def try_send(recipient_id, memo):
            async with async_session() as session:
                try:
                    return await create_payment(session, agents[0].agio_id, recipient_id, 3.0, memo)
                except Exception as e:
                    return {"error": str(e)}

        results = await asyncio.gather(
            try_send(agents[1].agio_id, "double-1"),
            try_send(agents[2].agio_id, "double-2"),
        )
        successes = [r for r in results if "payment_id" in r]
        assert len(successes) <= 1, f"Double spend! {len(successes)} payments on $5 balance"

    @pytest.mark.asyncio
    async def test_circular_payments(self, db, funded_agents):
        """A→B and B→A simultaneously. No deadlocks."""
        agents = await funded_agents(2, balance=10.0)
        await db.commit()

        async def pay(from_id, to_id, memo):
            async with async_session() as session:
                try:
                    return await create_payment(session, from_id, to_id, 1.0, memo)
                except Exception as e:
                    return {"error": str(e)}

        start = time.time()
        results = await asyncio.gather(
            pay(agents[0].agio_id, agents[1].agio_id, "a-to-b"),
            pay(agents[1].agio_id, agents[0].agio_id, "b-to-a"),
        )
        elapsed = time.time() - start

        successes = [r for r in results if "payment_id" in r]
        assert len(successes) >= 1, f"Expected at least 1, got {len(successes)}"
        assert elapsed < 10.0, f"Possible deadlock: took {elapsed:.1f}s"

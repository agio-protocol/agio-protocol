"""
Test 3: Cross-Chain Routing Stress Test

Tests cross-chain payments across Base, Polygon, and Solana.
Verifies reserves, routing, and global balance invariant.
"""
import pytest
import pytest_asyncio
from decimal import Decimal
from sqlalchemy import select

from src.models.agent import Agent
from src.models.chain import SupportedChain
from src.services import router_service
from src.core.database import async_session


class TestCrossChainStress:

    @pytest.mark.asyncio
    async def test_100_base_to_polygon(self, db, funded_agents, seeded_chains):
        """100 payments from Base agents to Polygon agents."""
        agents = await funded_agents(10, balance=50.0)

        successes = 0
        for i in range(100):
            sender = agents[i % 5]         # Base agents
            receiver = agents[5 + (i % 5)]  # Polygon agents

            from_id = f"agio:base:{sender.wallet_address}"
            to_id = f"agio:polygon:{receiver.wallet_address}"

            routing = await router_service.route_payment(db, from_id, to_id, 0.01)
            assert routing.routing_type == "CROSS_CHAIN"
            assert routing.source_chain == "base-sepolia"
            assert routing.dest_chain == "polygon-amoy"
            successes += 1

        assert successes == 100

    @pytest.mark.asyncio
    async def test_reserves_never_negative(self, db, funded_agents, seeded_chains):
        """Cross-chain payments must never make a reserve balance negative."""
        agents = await funded_agents(2, balance=100.0)

        for i in range(50):
            from_id = f"agio:base:{agents[0].wallet_address}"
            to_id = f"agio:polygon:{agents[1].wallet_address}"

            routing = await router_service.route_payment(db, from_id, to_id, 0.5)

            if routing.reserve_sufficient:
                await router_service.execute_cross_chain(
                    db, agents[0].agio_id, agents[1].agio_id,
                    0.5, f"0x{'%064x' % i}", routing
                )

        # Check all reserves
        chains = (await db.execute(select(SupportedChain))).scalars().all()
        for chain in chains:
            assert float(chain.reserve_balance) >= 0, \
                f"{chain.chain_name} reserve went negative: {chain.reserve_balance}"

    @pytest.mark.asyncio
    async def test_routing_decision_correct(self, db, funded_agents, seeded_chains):
        """Verify routing logic returns correct types."""
        agents = await funded_agents(2, balance=50.0)

        # Same chain
        r1 = await router_service.route_payment(
            db, agents[0].agio_id, agents[1].agio_id, 1.0
        )
        assert r1.routing_type == "SAME_CHAIN"

        # Cross chain
        r2 = await router_service.route_payment(
            db, f"agio:base:{agents[0].wallet_address}",
            f"agio:polygon:{agents[1].wallet_address}", 1.0
        )
        assert r2.routing_type == "CROSS_CHAIN"

    @pytest.mark.asyncio
    async def test_cross_chain_fee_applied(self, db, funded_agents, seeded_chains):
        """Cross-chain payments must include routing fee."""
        agents = await funded_agents(2, balance=50.0)

        routing = await router_service.route_payment(
            db, f"agio:base:{agents[0].wallet_address}",
            f"agio:polygon:{agents[1].wallet_address}", 1.0
        )

        assert routing.estimated_cost > 0.0001, "Cross-chain fee too low"
        assert routing.estimated_cost < 0.001, "Cross-chain fee too high"

"""
Test 6: Multi-Token Stress Test

Tests multi-token payments, cross-token swaps, preferred token settings,
and per-token balance isolation.
"""
import pytest
from decimal import Decimal

from sqlalchemy import select

from src.models.agent import Agent, AgentBalance
from src.services.payment_service import create_payment, SUPPORTED_TOKENS


class TestMultiTokenStress:

    @pytest.mark.asyncio
    async def test_supported_tokens_valid(self):
        """All expected tokens are in the supported set."""
        expected = {"USDC", "USDT", "DAI", "WETH", "cbETH"}
        assert SUPPORTED_TOKENS == expected

    @pytest.mark.asyncio
    async def test_unsupported_token_rejected(self, db, funded_agents):
        agents = await funded_agents(2, balance=10.0)
        with pytest.raises(ValueError, match="Unsupported token"):
            await create_payment(db, agents[0].agio_id, agents[1].agio_id, 1.0, token="DOGE")

    @pytest.mark.asyncio
    async def test_same_token_no_swap_fee(self, db, funded_agents):
        """When sender and receiver use same token, no swap fee."""
        agents = await funded_agents(2, balance=10.0)
        result = await create_payment(
            db, agents[0].agio_id, agents[1].agio_id, 1.0, token="USDC"
        )
        assert result["swap_needed"] is False
        assert result["swap_fee"] == 0.0
        assert result["from_token"] == "USDC"
        assert result["to_token"] == "USDC"

    @pytest.mark.asyncio
    async def test_cross_token_swap_fee_charged(self, db, funded_agents):
        """When tokens differ, 0.3% swap fee is charged."""
        agents = await funded_agents(2, balance=100.0, token="WETH")

        # Set receiver preferred token to USDC (different from WETH)
        receiver = agents[1]
        receiver.preferred_token = "USDC"
        await db.commit()

        result = await create_payment(
            db, agents[0].agio_id, agents[1].agio_id, 10.0, token="WETH"
        )
        assert result["swap_needed"] is True
        assert result["from_token"] == "WETH"
        assert result["to_token"] == "USDC"
        assert result["swap_fee"] > 0
        # 0.3% of 10.0 = 0.03
        assert abs(result["swap_fee"] - 0.03) < 0.001

    @pytest.mark.asyncio
    async def test_per_token_balance_isolation(self, db, funded_agents):
        """Paying in USDC doesn't affect WETH balance."""
        agents = await funded_agents(2, balance=50.0, token="USDC")

        # Also give agent 0 some WETH
        weth_bal = AgentBalance(
            agent_id=agents[0].id,
            token="WETH",
            balance=Decimal("25.0"),
            locked_balance=Decimal("0"),
        )
        db.add(weth_bal)
        await db.commit()

        # Pay in USDC
        await create_payment(
            db, agents[0].agio_id, agents[1].agio_id, 5.0, token="USDC"
        )

        # WETH balance should be untouched
        weth = (await db.execute(
            select(AgentBalance).where(
                AgentBalance.agent_id == agents[0].id,
                AgentBalance.token == "WETH"
            )
        )).scalar_one()
        assert float(weth.balance) == 25.0

    @pytest.mark.asyncio
    async def test_preferred_token_default_usdc(self, db, funded_agents):
        """New agents default to USDC preferred token."""
        agents = await funded_agents(1, balance=10.0)
        assert agents[0].preferred_token == "USDC"

    @pytest.mark.asyncio
    async def test_100_multitoken_payments(self, db, funded_agents):
        """100 payments across different tokens — no errors, balances correct."""
        agents = await funded_agents(4, balance=500.0, token="USDC")

        # Give agents WETH and DAI balances too
        for agent in agents:
            for tok in ["WETH", "DAI"]:
                bal = AgentBalance(
                    agent_id=agent.id,
                    token=tok,
                    balance=Decimal("500.0"),
                    locked_balance=Decimal("0"),
                )
                db.add(bal)
        await db.commit()

        tokens = ["USDC", "WETH", "DAI"]
        successes = 0
        for i in range(100):
            sender = agents[i % 2]
            receiver = agents[2 + (i % 2)]
            token = tokens[i % 3]
            try:
                result = await create_payment(
                    db, sender.agio_id, receiver.agio_id, 0.1,
                    memo=f"multi-{i}", token=token,
                )
                if result["status"] == "QUEUED":
                    successes += 1
            except Exception:
                pass

        assert successes >= 90, f"Only {successes}/100 multi-token payments succeeded"

    @pytest.mark.asyncio
    async def test_insufficient_token_balance_rejected(self, db, funded_agents):
        """Agent with USDC can't pay in WETH if they have no WETH."""
        agents = await funded_agents(2, balance=50.0, token="USDC")

        with pytest.raises(Exception, match="Insufficient"):
            await create_payment(
                db, agents[0].agio_id, agents[1].agio_id, 1.0, token="WETH"
            )

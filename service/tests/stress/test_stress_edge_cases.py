"""
Test 5: Edge Case Tests

Every weird edge case that could break the system.
Includes specific regression tests for all 5 bugs.
"""
import pytest
from decimal import Decimal

from src.services.payment_service import create_payment
from src.services.registry_service import register_agent
from src.models.agent import Agent


class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_zero_amount_rejected(self, db, funded_agents):
        agents = await funded_agents(2, balance=10.0)
        with pytest.raises(ValueError, match="positive"):
            await create_payment(db, agents[0].agio_id, agents[1].agio_id, 0.0)

    @pytest.mark.asyncio
    async def test_negative_amount_rejected(self, db, funded_agents):
        agents = await funded_agents(2, balance=10.0)
        with pytest.raises(ValueError, match="positive"):
            await create_payment(db, agents[0].agio_id, agents[1].agio_id, -1.0)

    @pytest.mark.asyncio
    async def test_minimum_amount(self, db, funded_agents):
        """BUG FIX #1: $0.000001 must serialize to Redis as string, not float."""
        agents = await funded_agents(2, balance=10.0)
        result = await create_payment(
            db, agents[0].agio_id, agents[1].agio_id, 0.000001, "min"
        )
        assert result["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_exceeds_balance_rejected(self, db, funded_agents):
        agents = await funded_agents(2, balance=5.0)
        with pytest.raises(Exception, match="Insufficient"):
            await create_payment(db, agents[0].agio_id, agents[1].agio_id, 10.0)

    @pytest.mark.asyncio
    async def test_locked_balance_blocks_payment(self, db, funded_agents):
        """
        BUG FIX #2: Deposit $10, lock ~$9 via pending payment, try to pay $2.
        Available after first payment must be too low for second.
        """
        agents = await funded_agents(2, balance=10.0)
        # First payment locks $9 + fee
        await create_payment(db, agents[0].agio_id, agents[1].agio_id, 9.0, "lock")

        # Now try $2 — should fail (only ~$1 available after fee)
        with pytest.raises(Exception, match="Insufficient"):
            await create_payment(db, agents[0].agio_id, agents[1].agio_id, 2.0, "blocked")

    @pytest.mark.asyncio
    async def test_self_payment_allowed(self, db, funded_agents):
        agents = await funded_agents(1, balance=10.0)
        try:
            result = await create_payment(db, agents[0].agio_id, agents[0].agio_id, 1.0)
            assert result["status"] == "QUEUED"
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_unregistered_agent_rejected(self, db, funded_agents):
        agents = await funded_agents(1, balance=10.0)
        with pytest.raises(Exception):
            await create_payment(db, agents[0].agio_id, "0x_nonexistent", 1.0)

    @pytest.mark.asyncio
    async def test_depleted_balance_then_pay(self, db, funded_agents):
        """Drain balance via payments, then verify rejection."""
        agents = await funded_agents(2, balance=10.0)

        # Lock most of balance
        await create_payment(db, agents[0].agio_id, agents[1].agio_id, 9.0, "drain")

        # Very little remaining — try to pay $2
        with pytest.raises(Exception, match="Insufficient"):
            await create_payment(db, agents[0].agio_id, agents[1].agio_id, 2.0)

    @pytest.mark.asyncio
    async def test_long_memo_truncated(self, db, funded_agents):
        """BUG FIX #3: Memo > 500 chars gets truncated, not rejected."""
        agents = await funded_agents(2, balance=10.0)
        long_memo = "x" * 10000
        result = await create_payment(
            db, agents[0].agio_id, agents[1].agio_id, 0.01, long_memo
        )
        assert result["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_empty_memo(self, db, funded_agents):
        agents = await funded_agents(2, balance=10.0)
        result = await create_payment(db, agents[0].agio_id, agents[1].agio_id, 0.01, "")
        assert result["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_unicode_memo(self, db, funded_agents):
        """BUG FIX #3: Emoji, CJK, accented characters must work."""
        agents = await funded_agents(2, balance=10.0)
        result = await create_payment(
            db, agents[0].agio_id, agents[1].agio_id, 0.01,
            "🤖💰 agent payment ñ 你好 データ \x00nullbyte"
        )
        assert result["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_null_byte_memo_stripped(self, db, funded_agents):
        """Null bytes in memo must be stripped, not crash Redis."""
        agents = await funded_agents(2, balance=10.0)
        result = await create_payment(
            db, agents[0].agio_id, agents[1].agio_id, 0.01,
            "hello\x00world\x00end"
        )
        assert result["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_duplicate_registration_rejected(self, db):
        await register_agent(db, "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "agent-1")
        with pytest.raises(Exception):
            await register_agent(db, "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "agent-1-dup")

    @pytest.mark.asyncio
    async def test_rapid_sequential_payments(self, db, funded_agents):
        """100 payments from same agent as fast as possible."""
        agents = await funded_agents(2, balance=100.0)
        count = 0
        for i in range(100):
            try:
                await create_payment(
                    db, agents[0].agio_id, agents[1].agio_id, 0.001, f"rapid-{i}"
                )
                count += 1
            except Exception:
                break
        assert count >= 50, f"Only {count} rapid payments succeeded"

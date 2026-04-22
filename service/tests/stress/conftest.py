"""
Shared fixtures for stress tests.

Uses a dedicated test database with NullPool + raw asyncpg cleanup.
This avoids ALL asyncpg "operation in progress" conflicts.
"""
import asyncio
import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text
from decimal import Decimal

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.models.base import Base
from src.models.agent import Agent, AgentBalance
from src.models.chain import SupportedChain
from src.models.loyalty import FeeTier


TEST_ENGINE = None
TEST_SESSION_FACTORY = None


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def init_test_infra():
    """One-time setup: create engine, tables, reinit Redis."""
    global TEST_ENGINE, TEST_SESSION_FACTORY

    TEST_ENGINE = create_async_engine(settings.database_url, poolclass=NullPool)
    TEST_SESSION_FACTORY = async_sessionmaker(TEST_ENGINE, class_=AsyncSession, expire_on_commit=False)

    async with TEST_ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    import redis.asyncio as aioredis
    import src.core.redis as redis_mod
    redis_mod.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    import src.core.database as db_mod
    db_mod.async_session = TEST_SESSION_FACTORY

    # Seed fee tiers (required for payment_service)
    async with TEST_SESSION_FACTORY() as session:
        from src.services.tier_service import seed_tiers
        await seed_tiers(session)

    yield


@pytest_asyncio.fixture
async def db():
    """Each test gets a clean DB via raw asyncpg TRUNCATE."""
    async with TEST_ENGINE.begin() as cleanup_conn:
        for table in reversed(Base.metadata.sorted_tables):
            await cleanup_conn.execute(sa_text(f'TRUNCATE TABLE "{table.name}" CASCADE'))

    import src.core.redis as redis_mod
    await redis_mod.redis_client.flushdb()

    # Re-seed fee tiers after truncation
    async with TEST_SESSION_FACTORY() as session:
        from src.services.tier_service import seed_tiers
        await seed_tiers(session)

    async with TEST_SESSION_FACTORY() as session:
        yield session


@pytest_asyncio.fixture
async def funded_agents(db):
    """Create agents with per-token balances (multi-token aware)."""
    async def _create(count: int, balance: float = 100.0, token: str = "USDC"):
        agents = []
        for i in range(count):
            agent = Agent(
                agio_id=f"0x{'%040x' % (i + 1)}",
                wallet_address=f"0x{'%040x' % (0x1000 + i)}",
                balance=Decimal(str(balance)),
                locked_balance=Decimal("0"),
                preferred_token="USDC",
            )
            db.add(agent)
            agents.append(agent)
        await db.flush()

        for agent in agents:
            agent_bal = AgentBalance(
                agent_id=agent.id,
                token=token,
                balance=Decimal(str(balance)),
                locked_balance=Decimal("0"),
            )
            db.add(agent_bal)

        await db.commit()
        for a in agents:
            await db.refresh(a)
        return agents
    return _create


@pytest_asyncio.fixture
async def seeded_chains(db):
    chains = [
        SupportedChain(chain_id=84532, chain_name="base-sepolia",
                       rpc_url="http://localhost:8545",
                       usdc_address="0x" + "f" * 40,
                       reserve_balance=Decimal("5000"), min_reserve=Decimal("1000"),
                       is_active=True),
        SupportedChain(chain_id=80002, chain_name="polygon-amoy",
                       rpc_url="https://rpc-amoy.polygon.technology",
                       usdc_address="0x" + "e" * 40,
                       reserve_balance=Decimal("3000"), min_reserve=Decimal("1000"),
                       is_active=True),
        SupportedChain(chain_id=11155111, chain_name="solana-devnet",
                       rpc_url="https://api.devnet.solana.com",
                       usdc_address="0x" + "d" * 40,
                       reserve_balance=Decimal("2000"), min_reserve=Decimal("1000"),
                       is_active=True),
    ]
    for c in chains:
        db.add(c)
    await db.commit()
    return chains

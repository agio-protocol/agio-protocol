#!/usr/bin/env python3
"""Batch worker runner for mainnet."""
import asyncio
import sys
sys.path.insert(0, "/Users/jeffreywylie/agio-protocol/service")

from src.core.config import settings
settings.rpc_url = "https://mainnet.base.org"
settings.vault_address = "0xe68bA48B4178a83212c00d6cb28c5A93Ec3FeEBc"
settings.batch_settlement_address = "0x3937a057AE18971657AD12830964511B73D9e7C5"
settings.database_url = "postgresql+asyncpg://agio:agio_dev_password@localhost:5432/agio_mainnet"
settings.redis_url = "redis://localhost:6379/1"
settings.max_batch_size = 500
settings.batch_interval_seconds = 60

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
import src.core.database as db_mod
engine = create_async_engine(settings.database_url, poolclass=NullPool)
db_mod.async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

import redis.asyncio as aioredis
import src.core.redis as redis_mod
redis_mod.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
redis_mod.PAYMENT_QUEUE = "agio:payment_queue"

from src.workers.batch_worker import run_worker
asyncio.run(run_worker())

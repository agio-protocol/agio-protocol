"""Production Base batch worker — reads config from environment variables."""
import asyncio
import logging

from ..core.config import settings
import src.core.database as db_mod

import redis.asyncio as aioredis
import src.core.redis as redis_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

redis_mod.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
redis_mod.PAYMENT_QUEUE = "agio:payment_queue"

from .batch_worker import run_worker

asyncio.run(run_worker())

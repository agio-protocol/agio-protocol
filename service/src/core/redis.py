# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Redis connection for payment queue."""
import redis.asyncio as aioredis
from .config import settings

redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

PAYMENT_QUEUE = "agio:payment_queue"

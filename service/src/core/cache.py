# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""Redis caching layer for expensive queries."""
import json
from .redis import redis_client

CACHE_TTL = {
    "feed": 60,
    "trending": 300,
    "jobs_search": 30,
    "leaderboard": 60,
    "network_stats": 60,
    "agent_profile": 300,
    "discover": 120,
}


async def get_cached(key: str):
    try:
        val = await redis_client.get(f"cache:{key}")
        return json.loads(val) if val else None
    except Exception:
        return None


async def set_cached(key: str, data, ttl_key: str = None, ttl: int = 60):
    try:
        t = CACHE_TTL.get(ttl_key, ttl)
        await redis_client.setex(f"cache:{key}", t, json.dumps(data, default=str))
    except Exception:
        pass


async def invalidate(pattern: str):
    try:
        keys = []
        async for key in redis_client.scan_iter(f"cache:{pattern}*"):
            keys.append(key)
        if keys:
            await redis_client.delete(*keys)
    except Exception:
        pass

# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""Rate limiting via Redis counters."""
from .redis import redis_client

MAX_REGISTRATIONS_PER_HOUR = 1000  # global
MAX_REGISTRATIONS_PER_IP_HOUR = 10


async def check_registration_limit(ip: str = "global") -> bool:
    """Returns True if under limit, False if rate exceeded."""
    try:
        global_key = "ratelimit:register:global"
        ip_key = f"ratelimit:register:ip:{ip}"

        global_count = int(await redis_client.get(global_key) or 0)
        if global_count >= MAX_REGISTRATIONS_PER_HOUR:
            return False

        ip_count = int(await redis_client.get(ip_key) or 0)
        if ip_count >= MAX_REGISTRATIONS_PER_IP_HOUR:
            return False

        pipe = redis_client.pipeline()
        pipe.incr(global_key)
        pipe.expire(global_key, 3600)
        pipe.incr(ip_key)
        pipe.expire(ip_key, 3600)
        await pipe.execute()
        return True
    except Exception:
        return True  # fail open on Redis error


async def check_rate(key: str, max_per_window: int, window_secs: int) -> bool:
    """Generic rate limiter. Returns True if under limit."""
    try:
        count = int(await redis_client.get(f"ratelimit:{key}") or 0)
        if count >= max_per_window:
            return False
        pipe = redis_client.pipeline()
        pipe.incr(f"ratelimit:{key}")
        pipe.expire(f"ratelimit:{key}", window_secs)
        await pipe.execute()
        return True
    except Exception:
        return True

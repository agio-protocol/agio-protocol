# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Centralized GMGN API client with rate limiting and Redis caching.

All workers should import from here instead of calling GMGN directly.
Enforces max 1 request per second globally via asyncio.Lock + Redis cache.
"""
import asyncio
import json
import logging
import os
import time
import uuid

import httpx

_log = logging.getLogger("gmgn-client")

GMGN_HOST = "https://openapi.gmgn.ai"
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")

_lock = asyncio.Lock()
_last_request_time = 0.0
MIN_REQUEST_INTERVAL = 1.0  # 1 second between requests


async def gmgn_get(path: str, params: dict = None, cache_ttl: int = 15) -> dict | None:
    """Rate-limited GMGN API call with Redis caching.

    Args:
        path: API path (e.g. "/v1/user/smartmoney")
        params: Query parameters
        cache_ttl: Cache TTL in seconds (0 to skip cache)
    """
    if not GMGN_API_KEY:
        return None

    params = params or {}
    cache_key = f"gmgn_cache:{path}:{json.dumps(params, sort_keys=True)}"

    # Check Redis cache first
    if cache_ttl > 0:
        try:
            from ..core.redis import redis_client
            cached = await redis_client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    # Rate limit — max 1 req/sec globally
    global _last_request_time
    async with _lock:
        now = time.time()
        wait = MIN_REQUEST_INTERVAL - (now - _last_request_time)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_time = time.time()

    # Make the request
    query = {**params, "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{GMGN_HOST}{path}", params=query,
                                    headers={"X-APIKEY": GMGN_API_KEY}, timeout=15)
            if resp.status_code == 429:
                _log.warning("GMGN rate limited (429)")
                await asyncio.sleep(30)
                return None
            if resp.status_code == 403:
                _log.warning("GMGN forbidden (403) — may be IP-level rate limit")
                return None
            if resp.status_code != 200:
                return None

            data = resp.json()

            # Cache the result
            if cache_ttl > 0:
                try:
                    from ..core.redis import redis_client
                    await redis_client.set(cache_key, json.dumps(data), ex=cache_ttl)
                except Exception:
                    pass

            return data

    except Exception as e:
        _log.debug(f"GMGN request failed: {e}")
        return None


async def get_smart_money_trades(chain: str = "sol", limit: int = 200) -> dict | None:
    return await gmgn_get("/v1/user/smartmoney", {"chain": chain, "limit": limit}, cache_ttl=15)


async def get_kol_trades(chain: str = "sol", limit: int = 100) -> dict | None:
    return await gmgn_get("/v1/user/kol", {"chain": chain, "limit": limit}, cache_ttl=15)


async def get_wallet_activities(wallet_address: str, chain: str = "sol", limit: int = 10) -> dict | None:
    return await gmgn_get("/v1/user/wallet_activities",
                          {"chain": chain, "wallet_address": wallet_address, "limit": limit},
                          cache_ttl=10)


async def get_wallet_stats(wallet_address: str, chain: str = "sol", period: str = "30d") -> dict | None:
    return await gmgn_get("/v1/user/wallet_stats",
                          {"chain": chain, "address": wallet_address, "period": period},
                          cache_ttl=300)


async def get_token_security(token_address: str, chain: str = "sol") -> dict | None:
    return await gmgn_get("/v1/token/security",
                          {"chain": chain, "address": token_address},
                          cache_ttl=60)

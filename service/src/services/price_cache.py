# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Shared price cache — all bots use this instead of hitting DexScreener directly.
Prices are cached in Redis for 10 seconds. One bot's lookup benefits all bots.
Global rate limiter prevents DexScreener 429s.
"""
import asyncio
import json
import logging
import time

import httpx

_log = logging.getLogger("price-cache")

_last_ds_call = 0.0
_ds_lock = asyncio.Lock()
_DS_MIN_INTERVAL = 0.5  # 500ms between DexScreener calls globally


async def get_price(token_address: str, max_age_seconds: int = 10) -> tuple[float, float]:
    """Get price and MC for a token. Returns (price_usd, mc_usd).
    Checks Redis cache first, falls back to DexScreener with rate limiting."""
    global _last_ds_call

    if not token_address:
        return (0.0, 0.0)

    # Check Redis cache
    try:
        from ..core.redis import redis_client
        cached = await redis_client.get(f"price:{token_address}")
        if cached:
            data = json.loads(cached)
            age = time.time() - data.get("ts", 0)
            if age < max_age_seconds:
                return (data.get("price", 0.0), data.get("mc", 0.0))
    except Exception:
        pass

    # Rate-limited DexScreener fetch
    async with _ds_lock:
        now = time.time()
        wait = _DS_MIN_INTERVAL - (now - _last_ds_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_ds_call = time.time()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{token_address}",
                timeout=5)
            if resp.status_code == 200:
                pairs = resp.json()
                if isinstance(pairs, list) and pairs:
                    p = pairs[0]
                    price = float(p.get("priceUsd", 0) or 0)
                    mc = float(p.get("marketCap", 0) or 0)

                    # Cache in Redis
                    try:
                        from ..core.redis import redis_client
                        await redis_client.set(
                            f"price:{token_address}",
                            json.dumps({"price": price, "mc": mc, "ts": time.time()}),
                            ex=30)
                    except Exception:
                        pass

                    return (price, mc)
            elif resp.status_code == 429:
                _log.debug(f"DexScreener 429 for {token_address[:16]}")
    except Exception as e:
        _log.debug(f"Price fetch error for {token_address[:16]}: {e}")

    # Fallback: Jupiter quote — works for any tradable token including pump.fun
    try:
        async with httpx.AsyncClient() as client:
            qr = await client.get("https://api.jup.ag/swap/v1/quote", params={
                "inputMint": token_address,
                "outputMint": "So11111111111111111111111111111111111111112",
                "amount": "1000000",
                "slippageBps": "1000",
            }, timeout=5)
            if qr.status_code == 200:
                out_lamports = int(qr.json().get("outAmount", 0))
                if out_lamports > 0:
                    sol_per_token = out_lamports / 1e9
                    try:
                        sr = await client.get(
                            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=3)
                        sol_usd = sr.json().get("solana", {}).get("usd", 86) if sr.status_code == 200 else 86
                    except Exception:
                        sol_usd = 86
                    price_usd = sol_per_token * sol_usd
                    if price_usd > 0:
                        try:
                            from ..core.redis import redis_client
                            await redis_client.set(
                                f"price:{token_address}",
                                json.dumps({"price": price_usd, "mc": 0, "ts": time.time()}),
                                ex=15)
                        except Exception:
                            pass
                        return (price_usd, 0.0)
    except Exception:
        pass

    return (0.0, 0.0)


async def get_pair_data(token_address: str, max_age_seconds: int = 10) -> dict | None:
    """Get full pair data for a token. Cached in Redis."""
    if not token_address:
        return None

    try:
        from ..core.redis import redis_client
        cached = await redis_client.get(f"pair:{token_address}")
        if cached:
            data = json.loads(cached)
            age = time.time() - data.get("_ts", 0)
            if age < max_age_seconds:
                return data
    except Exception:
        pass

    async with _ds_lock:
        now = time.time()
        wait = _DS_MIN_INTERVAL - (now - _last_ds_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_ds_call = time.time()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{token_address}",
                timeout=5)
            if resp.status_code == 200:
                pairs = resp.json()
                if isinstance(pairs, list) and pairs:
                    p = pairs[0]
                    p["_ts"] = time.time()
                    try:
                        from ..core.redis import redis_client
                        await redis_client.set(f"pair:{token_address}", json.dumps(p), ex=30)
                    except Exception:
                        pass
                    return p
    except Exception:
        pass

    return None

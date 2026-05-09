# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Historical memecoin deployer backfill — uses GMGN API for deployer data."""
import asyncio
import logging
import os
import time
import uuid
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.platform import MemeDeployment, TopDeployer

_log = logging.getLogger("meme-backfill")

GMGN_HOST = "https://openapi.gmgn.ai"
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")
MC_THRESHOLD = 1_000_000
BACKFILL_INTERVAL = 1800
GMGN_DELAY = 1.5


async def _gmgn_get(path: str, params: dict = None, client: httpx.AsyncClient = None) -> dict | None:
    """Call GMGN API with proper auth (API key + timestamp + client_id)."""
    if not GMGN_API_KEY:
        return None
    query = params or {}
    query["timestamp"] = int(time.time())
    query["client_id"] = str(uuid.uuid4())
    try:
        resp = await client.get(
            f"{GMGN_HOST}{path}",
            params=query,
            headers={"X-APIKEY": GMGN_API_KEY},
            timeout=15,
        )
        if resp.status_code == 429:
            _log.warning("GMGN rate limited, waiting 30s")
            await asyncio.sleep(30)
            return None
        if resp.status_code != 200:
            _log.debug(f"GMGN {path} returned {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        _log.debug(f"GMGN request failed: {e}")
        return None


def _calc_rating(tokens_over_1m: int, total_tokens: int, highest_mc: float, avg_peak: float, rug_count: int) -> str:
    rug_ratio = rug_count / max(total_tokens, 1)
    if rug_ratio >= 0.5 and total_tokens >= 3:
        return "D"
    if tokens_over_1m >= 5 and avg_peak >= 10_000_000:
        base = "S"
    elif tokens_over_1m >= 3 or avg_peak >= 5_000_000:
        base = "A"
    elif tokens_over_1m >= 2 or highest_mc >= 10_000_000:
        base = "B"
    else:
        base = "C"
    if rug_ratio >= 0.25 and total_tokens >= 3:
        downgrade = {"S": "A", "A": "B", "B": "C", "C": "C"}
        base = downgrade.get(base, base)
    return base


async def _enrich_token_via_gmgn(mint: str, client: httpx.AsyncClient) -> dict | None:
    """Get token info from GMGN including creator data."""
    data = await _gmgn_get(f"/v1/token/info", {"chain": "sol", "address": mint}, client)
    if not data:
        return None
    return data.get("data", data)


async def _enrich_existing_tokens():
    """Enrich tokens that are missing deployer data using GMGN."""
    async with async_session() as db:
        from sqlalchemy import or_
        tokens = (await db.execute(
            select(MemeDeployment)
            .where(or_(
                MemeDeployment.deployer_wallet.is_(None),
                MemeDeployment.deployer_wallet == "unknown",
            ))
            .order_by(MemeDeployment.peak_fdv.desc().nullslast())
            .limit(20)
        )).scalars().all()

        if not tokens:
            return

        _log.info(f"GMGN enriching {len(tokens)} tokens missing deployer data")
        async with httpx.AsyncClient() as client:
            for token in tokens:
                info = await _enrich_token_via_gmgn(token.mint_address, client)
                if not info:
                    await asyncio.sleep(GMGN_DELAY)
                    continue

                dev = info.get("dev", {})
                creator = dev.get("creator_address")

                if creator:
                    token.deployer_wallet = creator
                    token.deployer_token_count = dev.get("creator_open_count", 1) or 1

                    price = float(info.get("price", 0) or 0)
                    supply = float(info.get("circulating_supply", 0) or 0)
                    mc = price * supply if price and supply else float(info.get("market_cap", 0) or 0)
                    liq = float(info.get("liquidity", 0) or 0)

                    if mc > float(token.peak_fdv or 0):
                        token.peak_fdv = Decimal(str(mc))
                    token.fdv = Decimal(str(mc))
                    token.liquidity_usd = Decimal(str(liq))
                    if liq > float(token.peak_liquidity or 0):
                        token.peak_liquidity = Decimal(str(liq))
                    token.price_usd = Decimal(str(price))
                    token.last_updated = datetime.utcnow()

                    _log.info(f"Enriched {token.token_symbol}: creator={creator[:12]}... launched {dev.get('creator_open_count',0)} tokens, MC=${mc:,.0f}")

                    if mc >= MC_THRESHOLD:
                        await _update_top_deployer_via_gmgn(db, creator, token)
                else:
                    token.deployer_wallet = "unknown"

                await asyncio.sleep(GMGN_DELAY)

        await db.commit()


async def _discover_top_deployers_via_gmgn():
    """Use GMGN trending/new token feeds to find deployers with 1M+ hits."""
    # Get trending tokens from GMGN market
    async with httpx.AsyncClient() as client:
        data = await _gmgn_get("/v1/market/trending", {"chain": "sol"}, client)
    if not data:
        return

    tokens = data.get("data", data)
    if isinstance(tokens, dict):
        tokens = tokens.get("tokens", tokens.get("list", []))
    if not isinstance(tokens, list):
        return

    _log.info(f"GMGN trending: {len(tokens)} tokens to check")

    async with async_session() as db:
        for t in tokens[:30]:
            mint = t.get("address") or t.get("token_address") or t.get("mint", "")
            if not mint or len(mint) < 20:
                continue

            existing = (await db.execute(
                select(MemeDeployment).where(MemeDeployment.mint_address == mint)
            )).scalar_one_or_none()

            if existing and existing.deployer_wallet and existing.deployer_wallet != "unknown":
                continue

            async with httpx.AsyncClient() as enrich_client:
                info = await _enrich_token_via_gmgn(mint, enrich_client)
            if not info:
                await asyncio.sleep(GMGN_DELAY)
                continue

            dev = info.get("dev", {})
            creator = dev.get("creator_address")
            if not creator:
                await asyncio.sleep(GMGN_DELAY)
                continue

            price = float(info.get("price", 0) or 0)
            supply = float(info.get("circulating_supply", 0) or 0)
            mc = price * supply if price and supply else 0
            liq = float(info.get("liquidity", 0) or 0)
            symbol = info.get("symbol", "")[:20]
            name = info.get("name", "")[:100]

            if existing:
                existing.deployer_wallet = creator
                existing.deployer_token_count = dev.get("creator_open_count", 1) or 1
                existing.fdv = Decimal(str(mc))
                if mc > float(existing.peak_fdv or 0):
                    existing.peak_fdv = Decimal(str(mc))
                existing.liquidity_usd = Decimal(str(liq))
                existing.last_updated = datetime.utcnow()
            else:
                launch_ts = info.get("creation_timestamp") or info.get("open_timestamp")
                pair_created = datetime.utcfromtimestamp(launch_ts) if launch_ts else None

                deployment = MemeDeployment(
                    chain="solana", mint_address=mint,
                    deployer_wallet=creator,
                    token_name=name, token_symbol=symbol,
                    dex=info.get("launchpad", ""),
                    liquidity_usd=Decimal(str(liq)),
                    peak_liquidity=Decimal(str(liq)),
                    price_usd=Decimal(str(price)),
                    fdv=Decimal(str(mc)), peak_fdv=Decimal(str(mc)),
                    pair_address=info.get("biggest_pool_address", ""),
                    is_pump_fun=mint.endswith("pump") or info.get("launchpad", "") == "pump",
                    deployer_token_count=dev.get("creator_open_count", 1) or 1,
                    pair_created_at=pair_created,
                    last_updated=datetime.utcnow(),
                )
                db.add(deployment)
                _log.info(f"New from trending: {symbol} MC=${mc:,.0f} creator={creator[:12]}... ({dev.get('creator_open_count',0)} launches)")

            if mc >= MC_THRESHOLD and creator:
                await _update_top_deployer_via_gmgn(db, creator, None)

            await asyncio.sleep(GMGN_DELAY)

        await db.commit()


async def _update_top_deployer_via_gmgn(db: AsyncSession, wallet: str, trigger_token=None):
    """Update or create a top deployer entry using GMGN data."""
    tokens = (await db.execute(
        select(MemeDeployment).where(MemeDeployment.deployer_wallet == wallet)
    )).scalars().all()

    total = len(tokens)
    over_1m = [t for t in tokens if float(t.peak_fdv or 0) >= MC_THRESHOLD]
    rugs = [t for t in tokens if t.is_rugged]

    if not over_1m:
        return

    peaks = [float(t.peak_fdv or 0) for t in over_1m]
    highest = max(peaks)
    avg_peak = sum(peaks) / len(peaks)
    rug_count = len(rugs)
    rating = _calc_rating(len(over_1m), total, highest, avg_peak, rug_count)

    latest_launch = max((t.pair_created_at or t.created_at) for t in tokens)

    existing = (await db.execute(
        select(TopDeployer).where(TopDeployer.wallet == wallet)
    )).scalar_one_or_none()

    if existing:
        existing.total_tokens = total
        existing.tokens_over_1m = len(over_1m)
        existing.rug_count = rug_count
        existing.highest_mc = Decimal(str(highest))
        existing.avg_peak_mc = Decimal(str(avg_peak))
        existing.rating = rating
        existing.last_launch_at = latest_launch
        existing.last_updated = datetime.utcnow()
    else:
        deployer = TopDeployer(
            wallet=wallet, chain="solana",
            total_tokens=total, tokens_over_1m=len(over_1m),
            rug_count=rug_count,
            highest_mc=Decimal(str(highest)),
            avg_peak_mc=Decimal(str(avg_peak)),
            rating=rating, last_launch_at=latest_launch,
            last_updated=datetime.utcnow(),
        )
        db.add(deployer)
        _log.info(f"TOP DEPLOYER: {wallet[:12]}... | {len(over_1m)} hits, {rug_count} rugs, rating {rating}, best ${highest:,.0f}")


async def run():
    """Run GMGN-powered backfill — enrich existing tokens + discover from trending."""
    _log.info("GMGN backfill starting — enriching deployer database")
    await asyncio.sleep(45)

    while True:
        try:
            await _enrich_existing_tokens()
            await asyncio.sleep(30)
            await _discover_top_deployers_via_gmgn()
        except Exception as e:
            _log.error(f"GMGN backfill error: {e}")
        await asyncio.sleep(BACKFILL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run())

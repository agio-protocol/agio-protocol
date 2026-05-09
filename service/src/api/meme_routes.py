# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Meme deployer tracker API — top deployers, alerts, and live feed."""
from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..models.platform import MemeDeployment, TopDeployer

router = APIRouter(prefix="/v1/meme-tracker", tags=["meme-tracker"])


async def _require_auth(authorization: str):
    if not authorization or not authorization.startswith("Bearer ses_"):
        raise HTTPException(401, "Sign in to access the meme tracker")
    token = authorization.replace("Bearer ", "")
    from ..core.redis import redis_client
    session_data = await redis_client.get(f"session:{token}")
    if not session_data:
        raise HTTPException(401, "Session expired")


@router.get("/feed")
async def meme_feed(
    chain: str = Query("solana"),
    top_deployers_only: bool = Query(False),
    pump_fun_only: bool = Query(False),
    min_mc: float = Query(0),
    limit: int = Query(50, ge=1, le=200),
    before_id: int = Query(None),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Live feed of new token deployments. Requires auth."""
    await _require_auth(authorization)

    query = select(MemeDeployment).where(MemeDeployment.chain == chain)

    if top_deployers_only:
        top_wallets = (await db.execute(
            select(TopDeployer.wallet)
        )).scalars().all()
        if top_wallets:
            query = query.where(MemeDeployment.deployer_wallet.in_(top_wallets))
        else:
            return {"chain": chain, "total_tracked": 0, "count": 0, "deployments": []}

    if pump_fun_only:
        query = query.where(MemeDeployment.is_pump_fun == True)
    if min_mc > 0:
        query = query.where(MemeDeployment.peak_fdv >= min_mc)
    if before_id:
        query = query.where(MemeDeployment.id < before_id)

    query = query.order_by(MemeDeployment.created_at.desc()).limit(limit)
    deployments = (await db.execute(query)).scalars().all()

    # Check which deployers are top deployers
    top_wallet_set = set((await db.execute(select(TopDeployer.wallet))).scalars().all())

    return {
        "chain": chain,
        "count": len(deployments),
        "deployments": [
            {
                "id": d.id,
                "mint": d.mint_address,
                "deployer": d.deployer_wallet,
                "name": d.token_name,
                "symbol": d.token_symbol,
                "dex": d.dex,
                "liquidity_usd": float(d.liquidity_usd or 0),
                "price_usd": float(d.price_usd or 0),
                "current_mc": float(d.fdv or 0),
                "peak_mc": float(d.peak_fdv or 0),
                "pair_address": d.pair_address,
                "is_pump_fun": d.is_pump_fun,
                "is_rugged": d.is_rugged,
                "rugged_at": d.rugged_at.isoformat() if d.rugged_at else None,
                "peak_liquidity": float(d.peak_liquidity or 0),
                "is_top_deployer": d.deployer_wallet in top_wallet_set if d.deployer_wallet else False,
                "deployer_token_count": d.deployer_token_count or 1,
                "pair_created_at": d.pair_created_at.isoformat() if d.pair_created_at else None,
                "tracked_at": d.created_at.isoformat(),
            }
            for d in deployments
        ],
    }


@router.get("/top-deployers")
async def top_deployers(
    min_rating: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """List proven deployers — wallets with tokens that hit 1M+ MC."""
    await _require_auth(authorization)

    query = select(TopDeployer)
    if min_rating:
        rating_order = {"S": 4, "A": 3, "B": 2, "C": 1}
        min_val = rating_order.get(min_rating.upper(), 0)
        allowed = [r for r, v in rating_order.items() if v >= min_val]
        query = query.where(TopDeployer.rating.in_(allowed))

    query = query.order_by(TopDeployer.highest_mc.desc()).limit(limit)
    deployers = (await db.execute(query)).scalars().all()

    result = []
    for d in deployers:
        # Get all tokens by this deployer
        tokens = (await db.execute(
            select(MemeDeployment)
            .where(MemeDeployment.deployer_wallet == d.wallet)
            .order_by(MemeDeployment.peak_fdv.desc())
        )).scalars().all()

        rug_ratio = d.rug_count / max(d.total_tokens, 1)
        result.append({
            "wallet": d.wallet,
            "rating": d.rating,
            "total_tokens": d.total_tokens,
            "tokens_over_1m": d.tokens_over_1m,
            "rug_count": d.rug_count,
            "rug_ratio": round(rug_ratio, 2),
            "highest_mc": float(d.highest_mc or 0),
            "avg_peak_mc": float(d.avg_peak_mc or 0),
            "last_launch": d.last_launch_at.isoformat() if d.last_launch_at else None,
            "tokens": [
                {
                    "mint": t.mint_address,
                    "name": t.token_name,
                    "symbol": t.token_symbol,
                    "current_mc": float(t.fdv or 0),
                    "peak_mc": float(t.peak_fdv or 0),
                    "peak_liquidity": float(t.peak_liquidity or 0),
                    "current_liquidity": float(t.liquidity_usd or 0),
                    "dex": t.dex,
                    "is_pump_fun": t.is_pump_fun,
                    "is_rugged": t.is_rugged,
                    "launched": t.pair_created_at.isoformat() if t.pair_created_at else t.created_at.isoformat(),
                }
                for t in tokens
            ],
        })

    return {"deployers": result, "count": len(result)}


@router.get("/deployer/{wallet}")
async def deployer_detail(
    wallet: str,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Full history for a specific deployer."""
    await _require_auth(authorization)

    deployer = (await db.execute(
        select(TopDeployer).where(TopDeployer.wallet == wallet)
    )).scalar_one_or_none()

    tokens = (await db.execute(
        select(MemeDeployment)
        .where(MemeDeployment.deployer_wallet == wallet)
        .order_by(MemeDeployment.pair_created_at.desc().nullslast())
    )).scalars().all()

    if not tokens:
        raise HTTPException(404, "Deployer not found")

    peaks = [float(t.peak_fdv or 0) for t in tokens]
    over_1m = [p for p in peaks if p >= 1_000_000]

    return {
        "wallet": wallet,
        "rating": deployer.rating if deployer else "unrated",
        "total_tokens": len(tokens),
        "tokens_over_1m": len(over_1m),
        "highest_mc": max(peaks) if peaks else 0,
        "avg_peak_mc": sum(over_1m) / len(over_1m) if over_1m else 0,
        "tokens": [
            {
                "mint": t.mint_address,
                "name": t.token_name,
                "symbol": t.token_symbol,
                "current_mc": float(t.fdv or 0),
                "peak_mc": float(t.peak_fdv or 0),
                "liquidity_usd": float(t.liquidity_usd or 0),
                "dex": t.dex,
                "pair_address": t.pair_address,
                "is_pump_fun": t.is_pump_fun,
                "launched": t.pair_created_at.isoformat() if t.pair_created_at else t.created_at.isoformat(),
            }
            for t in tokens
        ],
    }


@router.get("/debug")
async def meme_debug(db: AsyncSession = Depends(get_db)):
    """Debug: show tokens over 1M and their deployer status."""
    tokens = (await db.execute(
        select(MemeDeployment)
        .where(MemeDeployment.peak_fdv >= 1_000_000)
        .order_by(MemeDeployment.peak_fdv.desc())
        .limit(20)
    )).scalars().all()
    return {
        "over_1m_tokens": [
            {
                "mint": t.mint_address[:16] + "...",
                "symbol": t.token_symbol,
                "deployer": t.deployer_wallet,
                "fdv": float(t.fdv or 0),
                "peak_fdv": float(t.peak_fdv or 0),
                "peak_liq": float(t.peak_liquidity or 0),
            }
            for t in tokens
        ],
    }


@router.get("/stats")
async def meme_stats(db: AsyncSession = Depends(get_db)):
    """Public stats — no auth required."""
    total = (await db.execute(
        select(func.count()).select_from(MemeDeployment)
    )).scalar() or 0
    top_count = (await db.execute(
        select(func.count()).select_from(TopDeployer)
    )).scalar() or 0
    pump_fun = (await db.execute(
        select(func.count()).select_from(MemeDeployment).where(MemeDeployment.is_pump_fun == True)
    )).scalar() or 0
    over_1m = (await db.execute(
        select(func.count()).select_from(MemeDeployment).where(MemeDeployment.peak_fdv >= 1_000_000)
    )).scalar() or 0

    return {
        "total_tokens_tracked": total,
        "top_deployers": top_count,
        "tokens_over_1m": over_1m,
        "pump_fun_tokens": pump_fun,
    }

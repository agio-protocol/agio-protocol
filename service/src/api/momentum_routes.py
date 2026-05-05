# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Momentum & Volume Scanner API — unusual volume spikes and price breakouts."""
import os
from fastapi import APIRouter, Depends, Query, Header, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta

from ..core.database import get_db

router = APIRouter(prefix="/v1/momentum", tags=["momentum"])


@router.get("/signals")
async def signals(
    signal_type: str = Query(None, description="Filter: volume_spike, momentum_up, momentum_down, breakout, dump"),
    strength: str = Query(None, description="Filter: MEDIUM, STRONG, VERY_STRONG"),
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """View recent momentum and volume signals."""
    from ..workers.momentum_scanner import MomentumSignal

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    query = select(MomentumSignal).where(MomentumSignal.detected_at >= cutoff)
    if signal_type:
        query = query.where(MomentumSignal.signal_type == signal_type)
    if strength:
        query = query.where(MomentumSignal.strength == strength.upper())
    query = query.order_by(MomentumSignal.detected_at.desc()).limit(limit)
    results = (await db.execute(query)).scalars().all()

    return {
        "count": len(results),
        "signals": [
            {
                "id": s.id,
                "symbol": s.symbol,
                "signal_type": s.signal_type,
                "strength": s.strength,
                "price": float(s.price),
                "market_cap": float(s.market_cap or 0),
                "volume_24h": float(s.volume_24h or 0),
                "volume_ratio": float(s.volume_ratio or 0),
                "pct_change_1h": float(s.pct_change_1h or 0),
                "pct_change_24h": float(s.pct_change_24h or 0),
                "pct_change_7d": float(s.pct_change_7d or 0),
                "description": s.description,
                "detected_at": s.detected_at.isoformat(),
            }
            for s in results
        ],
    }


@router.get("/hot")
async def hot_coins(
    hours: int = Query(6, ge=1, le=48),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Top coins by signal count — the hottest movers right now."""
    from ..workers.momentum_scanner import MomentumSignal

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    results = (await db.execute(
        select(
            MomentumSignal.symbol,
            func.count().label("signal_count"),
            func.max(MomentumSignal.pct_change_24h).label("max_24h"),
            func.max(MomentumSignal.volume_ratio).label("max_vol_ratio"),
            func.max(MomentumSignal.market_cap).label("mc"),
        )
        .where(MomentumSignal.detected_at >= cutoff)
        .group_by(MomentumSignal.symbol)
        .order_by(func.count().desc())
        .limit(limit)
    )).all()

    return {
        "period_hours": hours,
        "coins": [
            {
                "symbol": r.symbol,
                "signal_count": r.signal_count,
                "max_change_24h": float(r.max_24h or 0),
                "max_volume_ratio": float(r.max_vol_ratio or 0),
                "market_cap": float(r.mc or 0),
            }
            for r in results
        ],
    }


@router.get("/baselines")
async def baselines(
    symbol: str = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """View volume baselines used for spike detection."""
    from ..workers.momentum_scanner import VolumeBaseline

    query = select(VolumeBaseline)
    if symbol:
        query = query.where(VolumeBaseline.symbol == symbol.upper())
    query = query.order_by(VolumeBaseline.updated_at.desc()).limit(limit)
    results = (await db.execute(query)).scalars().all()

    return {
        "count": len(results),
        "baselines": [
            {
                "symbol": b.symbol,
                "avg_volume_7d": float(b.avg_volume_7d),
                "avg_mc": float(b.avg_mc or 0),
                "sample_count": b.sample_count,
                "updated_at": b.updated_at.isoformat(),
            }
            for b in results
        ],
    }


async def _require_admin(x_admin_key: str = Header(None)):
    admin_key = os.getenv("ADMIN_API_KEY", "")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(401, "Admin access required")


@router.get("/config")
async def get_config_endpoint(_=Depends(_require_admin)):
    from ..workers.momentum_scanner import get_config
    return await get_config()


@router.post("/config")
async def update_config(request: Request, _=Depends(_require_admin)):
    from ..workers.momentum_scanner import set_config, get_config
    body = await request.json()
    await set_config(body)
    return {"status": "updated", "config": await get_config()}

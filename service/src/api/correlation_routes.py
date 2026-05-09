# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Correlated Signals API — the alpha endpoint."""
from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import json as _json

from ..core.database import get_db

router = APIRouter(prefix="/v1/signals", tags=["signals"])


async def _require_auth(authorization: str):
    if not authorization or not authorization.startswith("Bearer ses_"):
        raise HTTPException(401, "Sign in to access signals")
    token = authorization.replace("Bearer ", "")
    from ..core.redis import redis_client
    if not await redis_client.get(f"session:{token}"):
        raise HTTPException(401, "Session expired")


@router.get("/correlated")
async def correlated_signals(
    min_confidence: int = Query(0, ge=0, le=100),
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(20, ge=1, le=100),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Correlated alpha signals — only fires when multiple sources agree."""
    await _require_auth(authorization)
    from ..workers.correlation_engine import CorrelatedSignal
    from datetime import datetime, timedelta

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    query = select(CorrelatedSignal).where(
        CorrelatedSignal.detected_at >= cutoff,
        CorrelatedSignal.confidence >= min_confidence,
    ).order_by(CorrelatedSignal.confidence.desc()).limit(limit)

    signals = (await db.execute(query)).scalars().all()

    return {
        "count": len(signals),
        "signals": [
            {
                "token_address": s.token_address,
                "token_symbol": s.token_symbol,
                "confidence": s.confidence,
                "source_count": s.source_count,
                "sources": _json.loads(s.sources_json) if s.sources_json else [],
                "mc_at_signal": float(s.mc_at_signal or 0),
                "price_at_signal": float(s.price_at_signal or 0) if s.price_at_signal else None,
                "pct_change_1h": float(s.pct_change_1h or 0) if s.pct_change_1h is not None else None,
                "pct_change_6h": float(s.pct_change_6h or 0) if s.pct_change_6h is not None else None,
                "pct_change_24h": float(s.pct_change_24h or 0) if s.pct_change_24h is not None else None,
                "outcome": s.outcome,
                "detected_at": s.detected_at.isoformat(),
            }
            for s in signals
        ],
    }


@router.get("/lunarcrush/{symbol}")
async def lunarcrush_data(symbol: str):
    """Get LunarCrush data for a coin — Galaxy Score, AltRank, sentiment."""
    import os, httpx
    key = os.getenv("LUNARCRUSH_API_KEY", "")
    if not key:
        return {"error": "LunarCrush not configured"}
    try:
        async with httpx.AsyncClient() as client:
            # Current data
            resp = await client.get(f"https://lunarcrush.com/api4/public/coins/{symbol.upper()}/v1",
                                    headers={"Authorization": f"Bearer {key}"}, timeout=10)
            if resp.status_code != 200:
                return {"error": f"LunarCrush returned {resp.status_code}"}
            coin = resp.json().get("data", {})

            # Time series for trend
            ts_resp = await client.get(
                f"https://lunarcrush.com/api4/public/coins/{symbol.upper()}/time-series/v2?bucket=hour&interval=24h",
                headers={"Authorization": f"Bearer {key}"}, timeout=10)
            ts_data = []
            if ts_resp.status_code == 200:
                ts_data = ts_resp.json().get("data", [])

            # Galaxy score trend
            gs_trend = [{"time": t.get("time"), "galaxy_score": t.get("galaxy_score"), "sentiment": t.get("sentiment")}
                        for t in ts_data[-24:] if t.get("galaxy_score")]

            return {
                "symbol": coin.get("symbol"),
                "name": coin.get("name"),
                "galaxy_score": coin.get("galaxy_score"),
                "alt_rank": coin.get("alt_rank"),
                "sentiment": coin.get("sentiment"),
                "volatility": coin.get("volatility"),
                "market_cap_rank": coin.get("market_cap_rank"),
                "percent_change_24h": coin.get("percent_change_24h"),
                "percent_change_7d": coin.get("percent_change_7d"),
                "percent_change_30d": coin.get("percent_change_30d"),
                "market_cap": coin.get("market_cap"),
                "volume_24h": coin.get("volume_24h"),
                "trend_24h": gs_trend,
            }
    except Exception as e:
        return {"error": str(e)}


@router.get("/correlated/stats")
async def correlated_stats(db: AsyncSession = Depends(get_db)):
    """Public stats for correlated signals."""
    from ..workers.correlation_engine import CorrelatedSignal

    total = (await db.execute(select(func.count()).select_from(CorrelatedSignal))).scalar() or 0
    scored = (await db.execute(
        select(func.count()).select_from(CorrelatedSignal).where(CorrelatedSignal.outcome.isnot(None))
    )).scalar() or 0
    wins = (await db.execute(
        select(func.count()).select_from(CorrelatedSignal).where(CorrelatedSignal.outcome.in_(["WIN", "BIG_WIN"]))
    )).scalar() or 0
    avg_conf = (await db.execute(
        select(func.avg(CorrelatedSignal.confidence))
    )).scalar() or 0
    high_conf = (await db.execute(
        select(func.count()).select_from(CorrelatedSignal).where(CorrelatedSignal.confidence >= 60)
    )).scalar() or 0

    return {
        "total_signals": total,
        "signals_scored": scored,
        "wins": wins,
        "win_rate": round(wins / max(scored, 1) * 100, 1),
        "avg_confidence": round(float(avg_conf), 1),
        "high_confidence_signals": high_conf,
    }

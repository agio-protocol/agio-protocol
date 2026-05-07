# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Sentiment Signal API — social convergence signals across Reddit, Telegram, CoinGecko."""
from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db

router = APIRouter(prefix="/v1/sentiment", tags=["sentiment"])


async def _require_auth(authorization: str):
    if not authorization or not authorization.startswith("Bearer ses_"):
        raise HTTPException(401, "Sign in to access sentiment signals")
    token = authorization.replace("Bearer ", "")
    from ..core.redis import redis_client
    if not await redis_client.get(f"session:{token}"):
        raise HTTPException(401, "Session expired")


@router.get("/signals")
async def sentiment_signals(
    category: str = Query(None),
    token: str = Query(None),
    min_strength: str = Query(None),
    limit: int = Query(20, ge=1, le=100),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Social convergence signals — tokens trending across multiple platforms."""
    await _require_auth(authorization)
    from ..workers.sentiment_tracker import SentimentSignal

    query = select(SentimentSignal)
    if category:
        query = query.where(SentimentSignal.category == category)
    if token:
        query = query.where(SentimentSignal.token_symbol == token.upper())
    if min_strength:
        strength_order = {"VERY_STRONG": 4, "STRONG": 3, "MEDIUM": 2}
        min_val = strength_order.get(min_strength.upper(), 0)
        allowed = [s for s, v in strength_order.items() if v >= min_val]
        query = query.where(SentimentSignal.strength.in_(allowed))

    query = query.order_by(SentimentSignal.detected_at.desc()).limit(limit)
    signals = (await db.execute(query)).scalars().all()

    return {
        "count": len(signals),
        "signals": [
            {
                "token_symbol": s.token_symbol,
                "token_name": s.token_name,
                "platform_count": s.platform_count,
                "platforms": s.platforms.split(",") if s.platforms else [],
                "total_mentions": s.total_mentions,
                "strength": s.strength,
                "has_smart_money": s.has_smart_money,
                "has_deployer": s.has_deployer,
                "description": s.description,
                "detected_at": s.detected_at.isoformat(),
            }
            for s in signals
        ],
    }


@router.get("/mentions")
async def recent_mentions(
    category: str = Query(None),
    token: str = Query(None),
    platform: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Recent social mentions across all platforms."""
    await _require_auth(authorization)
    from ..workers.sentiment_tracker import SocialMention

    query = select(SocialMention)
    if category:
        query = query.where(SocialMention.category == category)
    if token:
        query = query.where(SocialMention.token_symbol == token.upper())
    if platform:
        query = query.where(SocialMention.platform == platform)
    query = query.order_by(SocialMention.detected_at.desc()).limit(limit)
    mentions = (await db.execute(query)).scalars().all()

    return {
        "count": len(mentions),
        "mentions": [
            {
                "platform": m.platform,
                "token_symbol": m.token_symbol,
                "mention_count": m.mention_count,
                "source": m.source_detail,
                "text": m.sample_text,
                "sentiment": m.sentiment,
                "score": int(m.sentiment_score) if m.sentiment_score is not None else None,
                "conviction": int(m.conviction) if m.conviction is not None else None,
                "detected_at": m.detected_at.isoformat(),
            }
            for m in mentions
        ],
    }


@router.get("/buzz")
async def social_buzz(
    category: str = Query("meme"),
    hours: int = Query(6, ge=1, le=48),
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Public social buzz feed — real-time KOL mentions and trending tokens."""
    from ..workers.sentiment_tracker import SocialMention, SentimentSignal
    from datetime import datetime, timedelta

    cutoff = datetime.utcnow() - timedelta(hours=hours)

    mentions = (await db.execute(
        select(SocialMention)
        .where(SocialMention.detected_at >= cutoff, SocialMention.category == category)
        .order_by(SocialMention.detected_at.desc())
        .limit(limit)
    )).scalars().all()

    signals = (await db.execute(
        select(SentimentSignal)
        .where(SentimentSignal.detected_at >= cutoff, SentimentSignal.category == category)
        .order_by(SentimentSignal.detected_at.desc())
        .limit(10)
    )).scalars().all()

    top_tokens = (await db.execute(
        select(SocialMention.token_symbol, func.sum(SocialMention.mention_count).label("total"))
        .where(SocialMention.detected_at >= cutoff, SocialMention.category == category)
        .group_by(SocialMention.token_symbol)
        .order_by(func.sum(SocialMention.mention_count).desc())
        .limit(10)
    )).all()

    return {
        "category": category,
        "hours": hours,
        "trending": [{"symbol": t[0], "mentions": int(t[1])} for t in top_tokens],
        "signals": [
            {
                "token": s.token_symbol,
                "strength": s.strength,
                "platforms": s.platform_count,
                "mentions": s.total_mentions,
                "smart_money": s.has_smart_money,
                "description": s.description,
                "detected_at": s.detected_at.isoformat(),
            }
            for s in signals
        ],
        "feed": [
            {
                "platform": m.platform,
                "token": m.token_symbol,
                "mentions": m.mention_count,
                "source": m.source_detail,
                "text": (m.sample_text or "")[:200],
                "sentiment": m.sentiment,
                "score": int(m.sentiment_score) if m.sentiment_score is not None else None,
                "detected_at": m.detected_at.isoformat(),
            }
            for m in mentions
        ],
    }


@router.get("/stats")
async def sentiment_stats(category: str = Query(None), db: AsyncSession = Depends(get_db)):
    """Public sentiment stats. Optionally filter by category (meme/crypto/stocks)."""
    from ..workers.sentiment_tracker import SocialMention, SentimentSignal
    from datetime import datetime, timedelta

    cutoff = datetime.utcnow() - timedelta(hours=6)

    mention_q = select(func.count()).select_from(SocialMention).where(SocialMention.detected_at >= cutoff)
    signal_q = select(func.count()).select_from(SentimentSignal)
    strong_q = select(func.count()).select_from(SentimentSignal).where(SentimentSignal.strength.in_(["STRONG", "VERY_STRONG"]))
    conv_q = select(func.count()).select_from(SentimentSignal).where(SentimentSignal.has_smart_money == True)
    top_q = select(SocialMention.token_symbol, func.sum(SocialMention.mention_count).label("total")).where(SocialMention.detected_at >= cutoff)

    if category:
        mention_q = mention_q.where(SocialMention.category == category)
        signal_q = signal_q.where(SentimentSignal.category == category)
        strong_q = strong_q.where(SentimentSignal.category == category)
        conv_q = conv_q.where(SentimentSignal.category == category)
        top_q = top_q.where(SocialMention.category == category)

    total_mentions = (await db.execute(mention_q)).scalar() or 0
    total_signals = (await db.execute(signal_q)).scalar() or 0
    strong_signals = (await db.execute(strong_q)).scalar() or 0
    convergence = (await db.execute(conv_q)).scalar() or 0

    top_tokens = (await db.execute(
        top_q.group_by(SocialMention.token_symbol)
        .order_by(func.sum(SocialMention.mention_count).desc())
        .limit(10)
    )).all()

    return {
        "category": category or "all",
        "mentions_6h": total_mentions,
        "total_signals": total_signals,
        "strong_signals": strong_signals,
        "convergence_signals": convergence,
        "top_tokens_6h": [{"symbol": t[0], "mentions": int(t[1])} for t in top_tokens],
    }

# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Wallet Follow API — followed wallet signals, leaderboard, and management."""
from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import json as _json

from ..core.database import get_db

router = APIRouter(prefix="/v1/wallet-follow", tags=["wallet-follow"])


@router.get("/signals")
async def follow_signals(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Recent wallet follow cluster signals — public stats."""
    from ..workers.wallet_follow import WalletSignal
    signals = (await db.execute(
        select(WalletSignal).order_by(WalletSignal.detected_at.desc()).limit(limit)
    )).scalars().all()
    return {
        "count": len(signals),
        "signals": [
            {
                "token_address": s.token_address,
                "token_symbol": s.token_symbol,
                "wallet_count": s.wallet_count,
                "total_usd": float(s.total_usd or 0),
                "avg_wallet_score": float(s.avg_wallet_score or 0),
                "strength": s.strength,
                "wallets": _json.loads(s.wallets_json) if s.wallets_json else [],
                "description": s.description,
                "detected_at": s.detected_at.isoformat(),
            }
            for s in signals
        ],
    }


@router.get("/leaderboard")
async def follow_leaderboard(
    limit: int = Query(30, ge=1, le=200),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Followed wallet leaderboard ranked by score."""
    from ..workers.wallet_follow import FollowedWallet
    wallets = (await db.execute(
        select(FollowedWallet)
        .where(FollowedWallet.active == True, FollowedWallet.score > 0)
        .order_by(FollowedWallet.score.desc())
        .limit(limit)
    )).scalars().all()
    return {
        "count": len(wallets),
        "wallets": [
            {
                "wallet": w.wallet,
                "label": w.label,
                "twitter": w.twitter,
                "tier": w.tier,
                "score": float(w.score or 0),
                "winrate": float(w.winrate or 0),
                "realized_profit": float(w.realized_profit or 0),
                "total_trades": w.total_trades,
                "tokens_traded": w.tokens_traded,
                "pnl_2x_plus": w.pnl_2x_plus,
                "pnl_5x_plus": w.pnl_5x_plus,
                "source": w.source,
                "auto_discovered": w.auto_discovered,
            }
            for w in wallets
        ],
    }


@router.get("/stats")
async def follow_stats(db: AsyncSession = Depends(get_db)):
    """Public stats."""
    from ..workers.wallet_follow import FollowedWallet, WalletTrade, WalletSignal
    total = (await db.execute(select(func.count()).select_from(FollowedWallet).where(FollowedWallet.active == True))).scalar() or 0
    scored = (await db.execute(select(func.count()).select_from(FollowedWallet).where(FollowedWallet.score > 0))).scalar() or 0
    trades = (await db.execute(select(func.count()).select_from(WalletTrade))).scalar() or 0
    signals = (await db.execute(select(func.count()).select_from(WalletSignal))).scalar() or 0
    return {"wallets_followed": total, "wallets_scored": scored, "trades_tracked": trades, "signals": signals}


@router.post("/add")
async def add_wallet_endpoint(
    wallet: str = Query(...),
    label: str = Query(""),
    twitter: str = Query(""),
    source: str = Query("manual"),
    x_admin_key: str = Header(None),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Add a wallet to follow. Accepts admin key or Bearer auth."""
    import os
    admin_key = os.getenv("ADMIN_API_KEY", "")
    is_admin = admin_key and x_admin_key == admin_key
    is_auth = authorization and authorization.startswith("Bearer ses_")
    if not is_admin and not is_auth:
        raise HTTPException(401, "Sign in or use admin key to add wallets")
    from ..workers.wallet_follow import add_wallet
    result = await add_wallet(wallet, label=label, source=source, twitter=twitter)
    return {"status": "added", "wallet": wallet, "label": label}

# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Smart Money Tracker API — wallet leaderboard, cluster signals, and trade feed."""
from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import json as _json

from ..core.database import get_db

router = APIRouter(prefix="/v1/smart-money", tags=["smart-money"])


async def _require_auth(authorization: str):
    if not authorization or not authorization.startswith("Bearer ses_"):
        raise HTTPException(401, "Sign in to access the smart money tracker")
    token = authorization.replace("Bearer ", "")
    from ..core.redis import redis_client
    session_data = await redis_client.get(f"session:{token}")
    if not session_data:
        raise HTTPException(401, "Session expired")


@router.get("/leaderboard")
async def smart_money_leaderboard(
    min_tier: str = Query(None),
    min_winrate: float = Query(0),
    limit: int = Query(50, ge=1, le=200),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Top smart money wallets ranked by score."""
    await _require_auth(authorization)
    from ..workers.smart_money_tracker import SmartMoneyWallet

    query = select(SmartMoneyWallet).where(SmartMoneyWallet.score > 0)

    if min_tier:
        tier_order = {"S": 4, "A": 3, "B": 2, "C": 1}
        min_val = tier_order.get(min_tier.upper(), 0)
        allowed = [t for t, v in tier_order.items() if v >= min_val]
        query = query.where(SmartMoneyWallet.tier.in_(allowed))

    if min_winrate > 0:
        query = query.where(SmartMoneyWallet.winrate >= min_winrate)

    query = query.order_by(SmartMoneyWallet.score.desc()).limit(limit)
    wallets = (await db.execute(query)).scalars().all()

    return {
        "count": len(wallets),
        "wallets": [
            {
                "wallet": w.wallet,
                "name": w.name,
                "twitter": w.twitter,
                "tier": w.tier,
                "score": float(w.score or 0),
                "winrate": float(w.winrate or 0),
                "realized_profit": float(w.realized_profit or 0),
                "total_trades": w.total_trades,
                "tokens_traded": w.tokens_traded,
                "pnl_2x_plus": w.pnl_2x_plus,
                "pnl_5x_plus": w.pnl_5x_plus,
                "avg_holding_period_seconds": float(w.avg_holding_period or 0),
                "funded_from": w.funded_from,
                "tags": w.tags.split(",") if w.tags else [],
                "last_active": w.last_active.isoformat() if w.last_active else None,
            }
            for w in wallets
        ],
    }


@router.get("/clusters")
async def cluster_signals(
    min_strength: str = Query(None),
    limit: int = Query(20, ge=1, le=100),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Recent cluster signals — multiple smart money wallets buying the same token."""
    await _require_auth(authorization)
    from ..workers.smart_money_tracker import ClusterSignal

    query = select(ClusterSignal)

    if min_strength:
        strength_order = {"VERY_STRONG": 4, "STRONG": 3, "MEDIUM": 2, "WEAK": 1}
        min_val = strength_order.get(min_strength.upper(), 0)
        allowed = [s for s, v in strength_order.items() if v >= min_val]
        query = query.where(ClusterSignal.signal_strength.in_(allowed))

    query = query.order_by(ClusterSignal.detected_at.desc()).limit(limit)
    signals = (await db.execute(query)).scalars().all()

    return {
        "count": len(signals),
        "signals": [
            {
                "id": s.id,
                "token_address": s.token_address,
                "token_symbol": s.token_symbol,
                "wallet_count": s.wallet_count,
                "total_usd": float(s.total_usd or 0),
                "full_position_count": s.full_position_count,
                "kol_count": s.kol_count,
                "signal_strength": s.signal_strength,
                "wallets": _json.loads(s.wallets_json) if s.wallets_json else [],
                "weighted_score": float(s.weighted_score or 0) if hasattr(s, 'weighted_score') else 0,
                "avg_wallet_winrate": float(s.avg_wallet_winrate or 0) if hasattr(s, 'avg_wallet_winrate') else 0,
                "is_deployer_token": getattr(s, 'is_deployer_token', False),
                "price_at_signal": float(s.price_at_signal or 0) if hasattr(s, 'price_at_signal') and s.price_at_signal else None,
                "pct_change_1h": float(s.pct_change_1h or 0) if hasattr(s, 'pct_change_1h') and s.pct_change_1h else None,
                "pct_change_6h": float(s.pct_change_6h or 0) if hasattr(s, 'pct_change_6h') and s.pct_change_6h else None,
                "pct_change_24h": float(s.pct_change_24h or 0) if hasattr(s, 'pct_change_24h') and s.pct_change_24h else None,
                "outcome": getattr(s, 'outcome', None),
                "detected_at": s.detected_at.isoformat(),
            }
            for s in signals
        ],
    }


@router.get("/trades")
async def smart_money_trades(
    side: str = Query(None),
    token: str = Query(None),
    wallet: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Recent smart money trades."""
    await _require_auth(authorization)
    from ..workers.smart_money_tracker import SmartMoneyTrade

    query = select(SmartMoneyTrade)
    if side:
        query = query.where(SmartMoneyTrade.side == side)
    if token:
        query = query.where(SmartMoneyTrade.token_address == token)
    if wallet:
        query = query.where(SmartMoneyTrade.wallet == wallet)

    query = query.order_by(SmartMoneyTrade.trade_time.desc()).limit(limit)
    trades = (await db.execute(query)).scalars().all()

    return {
        "count": len(trades),
        "trades": [
            {
                "tx_hash": t.tx_hash,
                "wallet": t.wallet,
                "wallet_name": getattr(t, 'wallet_name', None) or "",
                "wallet_twitter": getattr(t, 'wallet_twitter', None) or "",
                "token_address": t.token_address,
                "token_symbol": t.token_symbol,
                "side": t.side,
                "amount_usd": float(t.amount_usd or 0),
                "price_usd": float(t.price_usd or 0),
                "is_full_position": t.is_full_position,
                "is_kol": t.is_kol,
                "trade_time": t.trade_time.isoformat(),
            }
            for t in trades
        ],
    }


@router.get("/wallet/{wallet_address}")
async def wallet_detail(
    wallet_address: str,
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Detailed view of a specific smart money wallet."""
    await _require_auth(authorization)
    from ..workers.smart_money_tracker import SmartMoneyWallet, SmartMoneyTrade

    w = (await db.execute(
        select(SmartMoneyWallet).where(SmartMoneyWallet.wallet == wallet_address)
    )).scalar_one_or_none()

    recent_trades = (await db.execute(
        select(SmartMoneyTrade)
        .where(SmartMoneyTrade.wallet == wallet_address)
        .order_by(SmartMoneyTrade.trade_time.desc())
        .limit(50)
    )).scalars().all()

    return {
        "wallet": wallet_address,
        "profile": {
            "name": w.name if w else None,
            "twitter": w.twitter if w else None,
            "tier": w.tier if w else "unknown",
            "score": float(w.score or 0) if w else 0,
            "winrate": float(w.winrate or 0) if w else 0,
            "realized_profit": float(w.realized_profit or 0) if w else 0,
            "total_trades": w.total_trades if w else 0,
            "tokens_traded": w.tokens_traded if w else 0,
            "pnl_2x_plus": w.pnl_2x_plus if w else 0,
            "pnl_5x_plus": w.pnl_5x_plus if w else 0,
            "funded_from": w.funded_from if w else None,
            "tags": w.tags.split(",") if w and w.tags else [],
        } if w else None,
        "recent_trades": [
            {
                "tx_hash": t.tx_hash,
                "token_address": t.token_address,
                "token_symbol": t.token_symbol,
                "side": t.side,
                "amount_usd": float(t.amount_usd or 0),
                "is_full_position": t.is_full_position,
                "trade_time": t.trade_time.isoformat(),
            }
            for t in recent_trades
        ],
    }


@router.get("/stats")
async def smart_money_stats(db: AsyncSession = Depends(get_db)):
    """Public stats — no auth required."""
    from ..workers.smart_money_tracker import SmartMoneyWallet, SmartMoneyTrade, ClusterSignal

    total_wallets = (await db.execute(
        select(func.count()).select_from(SmartMoneyWallet)
    )).scalar() or 0
    scored_wallets = (await db.execute(
        select(func.count()).select_from(SmartMoneyWallet).where(SmartMoneyWallet.score > 0)
    )).scalar() or 0
    total_trades = (await db.execute(
        select(func.count()).select_from(SmartMoneyTrade)
    )).scalar() or 0
    total_clusters = (await db.execute(
        select(func.count()).select_from(ClusterSignal)
    )).scalar() or 0
    strong_clusters = (await db.execute(
        select(func.count()).select_from(ClusterSignal)
        .where(ClusterSignal.signal_strength.in_(["STRONG", "VERY_STRONG"]))
    )).scalar() or 0

    # Accuracy stats
    scored_signals = (await db.execute(
        select(func.count()).select_from(ClusterSignal).where(ClusterSignal.outcome.isnot(None))
    )).scalar() or 0
    winning_signals = (await db.execute(
        select(func.count()).select_from(ClusterSignal)
        .where(ClusterSignal.outcome.in_(["WIN", "BIG_WIN"]))
    )).scalar() or 0
    accuracy = round(winning_signals / max(scored_signals, 1) * 100, 1)

    return {
        "wallets_tracked": total_wallets,
        "wallets_scored": scored_wallets,
        "trades_recorded": total_trades,
        "cluster_signals": total_clusters,
        "strong_signals": strong_clusters,
        "signals_scored": scored_signals,
        "signals_won": winning_signals,
        "accuracy_pct": accuracy,
    }


@router.get("/signal-audit")
async def signal_audit(
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Admin: audit signal accuracy — shows price at signal vs current price."""
    from ..workers.smart_money_tracker import ClusterSignal
    import httpx

    signals = (await db.execute(
        select(ClusterSignal)
        .where(ClusterSignal.price_at_signal.isnot(None))
        .order_by(ClusterSignal.detected_at.desc())
        .limit(limit)
    )).scalars().all()

    results = []
    async with httpx.AsyncClient() as client:
        for s in signals[:20]:
            current_price = None
            current_mc = None
            try:
                resp = await client.get(
                    f"https://api.dexscreener.com/token-pairs/v1/solana/{s.token_address}",
                    timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    pairs = data if isinstance(data, list) else data.get("pairs", [])
                    if pairs:
                        current_price = float(pairs[0].get("priceUsd", 0) or 0)
                        current_mc = float(pairs[0].get("fdv", 0) or 0)
            except:
                pass

            signal_price = float(s.price_at_signal or 0)
            pct = ((current_price - signal_price) / signal_price * 100) if signal_price and current_price else None

            results.append({
                "symbol": s.token_symbol,
                "strength": s.signal_strength,
                "wallets": s.wallet_count,
                "signal_price": signal_price,
                "current_price": current_price,
                "pct_change": round(pct, 1) if pct is not None else None,
                "mc_at_signal": float(s.mc_at_signal) if s.mc_at_signal else None,
                "current_mc": current_mc,
                "highest_mc": float(s.highest_mc) if s.highest_mc else None,
                "mc_multiple": round(float(s.highest_mc) / float(s.mc_at_signal), 1) if s.highest_mc and s.mc_at_signal and float(s.mc_at_signal) > 0 else None,
                "detected_at": s.detected_at.isoformat(),
                "outcome": "WIN" if pct and pct > 10 else "LOSS" if pct and pct < -10 else "NEUTRAL" if pct is not None else "UNKNOWN",
            })

            await __import__('asyncio').sleep(0.3)

    wins = sum(1 for r in results if r["outcome"] == "WIN")
    losses = sum(1 for r in results if r["outcome"] == "LOSS")
    scored = sum(1 for r in results if r["outcome"] != "UNKNOWN")

    return {
        "total_audited": len(results),
        "wins": wins,
        "losses": losses,
        "neutral": scored - wins - losses,
        "win_rate": round(wins / max(scored, 1) * 100, 1),
        "signals": results,
    }

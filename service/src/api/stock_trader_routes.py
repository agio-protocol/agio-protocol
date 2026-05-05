# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Stock Paper Trading Bot API — institutional-grade stock swing trades."""
import os
from fastapi import APIRouter, Depends, Query, Header, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from decimal import Decimal

from ..core.database import get_db

router = APIRouter(prefix="/v1/stock-trader", tags=["stock-trader"])


async def _require_admin(x_admin_key: str = Header(None)):
    admin_key = os.getenv("ADMIN_API_KEY", "")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(401, "Admin access required")


@router.get("/performance")
async def performance(db: AsyncSession = Depends(get_db)):
    """Public performance dashboard for stock trades."""
    from ..workers.stock_paper_trader import StockPaperPosition

    total = (await db.execute(select(func.count()).select_from(StockPaperPosition))).scalar() or 0
    open_pos = (await db.execute(
        select(func.count()).select_from(StockPaperPosition).where(StockPaperPosition.status == "OPEN")
    )).scalar() or 0
    closed = (await db.execute(
        select(func.count()).select_from(StockPaperPosition).where(StockPaperPosition.status == "CLOSED")
    )).scalar() or 0
    winners = (await db.execute(
        select(func.count()).select_from(StockPaperPosition)
        .where(StockPaperPosition.status == "CLOSED", StockPaperPosition.pnl_pct > 0)
    )).scalar() or 0

    total_pnl = (await db.execute(
        select(func.sum(StockPaperPosition.pnl_usd)).where(StockPaperPosition.status == "CLOSED")
    )).scalar() or 0

    best = (await db.execute(
        select(StockPaperPosition).where(StockPaperPosition.status == "CLOSED")
        .order_by(StockPaperPosition.pnl_pct.desc()).limit(1)
    )).scalar_one_or_none()
    worst = (await db.execute(
        select(StockPaperPosition).where(StockPaperPosition.status == "CLOSED")
        .order_by(StockPaperPosition.pnl_pct.asc()).limit(1)
    )).scalar_one_or_none()

    avg_hold = (await db.execute(
        select(func.avg(
            func.extract('epoch', StockPaperPosition.closed_at - StockPaperPosition.opened_at) / 86400
        )).where(StockPaperPosition.status == "CLOSED", StockPaperPosition.closed_at.isnot(None))
    )).scalar()

    return {
        "market": "stocks",
        "total_trades": total,
        "open_positions": open_pos,
        "closed_positions": closed,
        "winners": winners,
        "losers": closed - winners,
        "win_rate": round(winners / max(closed, 1) * 100, 1),
        "total_pnl_usd": round(float(total_pnl), 2),
        "best_trade": {
            "ticker": best.ticker, "pnl_pct": float(best.pnl_pct or 0),
        } if best else None,
        "worst_trade": {
            "ticker": worst.ticker, "pnl_pct": float(worst.pnl_pct or 0),
        } if worst else None,
        "avg_hold_days": round(float(avg_hold or 0), 1),
    }


@router.get("/positions")
async def positions(
    status: str = Query("OPEN"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """View current and past stock positions."""
    from ..workers.stock_paper_trader import StockPaperPosition

    query = select(StockPaperPosition)
    if status:
        query = query.where(StockPaperPosition.status == status.upper())
    query = query.order_by(StockPaperPosition.opened_at.desc()).limit(limit)
    positions = (await db.execute(query)).scalars().all()

    return {
        "count": len(positions),
        "positions": [
            {
                "id": p.id,
                "ticker": p.ticker,
                "company_name": p.company_name,
                "entry_price": float(p.entry_price),
                "current_price": float(p.current_price or 0),
                "highest_price": float(p.highest_price or 0),
                "position_size": float(p.position_size_usd),
                "remaining_pct": float(p.remaining_pct),
                "pnl_pct": float(p.pnl_pct or 0),
                "pnl_usd": float(p.pnl_usd or 0),
                "agiotage_score": p.agiotage_score,
                "sources": p.entry_sources,
                "signal_strength": p.entry_signal,
                "status": p.status,
                "close_reason": p.close_reason,
                "opened_at": p.opened_at.isoformat(),
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            }
            for p in positions
        ],
    }


@router.get("/trades")
async def trades(
    position_id: int = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """View individual stock trades."""
    from ..workers.stock_paper_trader import StockPaperTrade

    query = select(StockPaperTrade)
    if position_id:
        query = query.where(StockPaperTrade.position_id == position_id)
    query = query.order_by(StockPaperTrade.executed_at.desc()).limit(limit)
    trades = (await db.execute(query)).scalars().all()

    return {
        "count": len(trades),
        "trades": [
            {
                "id": t.id,
                "position_id": t.position_id,
                "action": t.action,
                "pct_of_position": float(t.pct_of_position),
                "price": float(t.price),
                "usd_value": float(t.usd_value or 0),
                "pnl_pct": float(t.pnl_pct or 0),
                "reason": t.reason,
                "executed_at": t.executed_at.isoformat(),
            }
            for t in trades
        ],
    }


@router.get("/config")
async def get_config_endpoint(_=Depends(_require_admin)):
    from ..workers.stock_paper_trader import get_config
    return await get_config()


@router.post("/config")
async def update_config(request: Request, _=Depends(_require_admin)):
    from ..workers.stock_paper_trader import set_config, get_config
    body = await request.json()
    await set_config(body)
    return {"status": "updated", "config": await get_config()}


@router.post("/close/{position_id}")
async def force_close(position_id: int, _=Depends(_require_admin), db: AsyncSession = Depends(get_db)):
    """Force close a stock position."""
    from ..workers.stock_paper_trader import StockPaperPosition, StockPaperTrade, _get_stock_price
    from datetime import datetime

    pos = (await db.execute(
        select(StockPaperPosition).where(StockPaperPosition.id == position_id)
    )).scalar_one_or_none()
    if not pos:
        raise HTTPException(404, "Position not found")
    if pos.status != "OPEN":
        raise HTTPException(400, "Position already closed")

    price = await _get_stock_price(pos.ticker)
    pnl_pct = ((price - float(pos.entry_price)) / float(pos.entry_price)) * 100 if price > 0 else 0

    remaining = float(pos.remaining_pct)
    usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)

    trade = StockPaperTrade(
        position_id=pos.id, action="SELL",
        pct_of_position=Decimal(str(remaining)),
        price=Decimal(str(price)),
        usd_value=Decimal(str(round(usd_val, 2))),
        pnl_pct=Decimal(str(round(pnl_pct, 4))),
        reason="Manual close by admin",
    )
    db.add(trade)
    pos.remaining_pct = Decimal("0")
    pos.status = "CLOSED"
    pos.close_reason = f"Manual close ({pnl_pct:.1f}%)"
    pos.closed_at = datetime.utcnow()
    pos.current_price = Decimal(str(price))
    pos.pnl_pct = Decimal(str(round(pnl_pct, 4)))
    pos.pnl_usd = Decimal(str(round(usd_val - float(pos.position_size_usd) * remaining / 100, 2)))
    await db.commit()

    return {"status": "closed", "position_id": position_id, "pnl_pct": pnl_pct}

# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Knowledge Marketplace API — agents buy and sell data, models, tools."""
from decimal import Decimal
from fastapi import APIRouter, Depends, Query, Header, HTTPException, Request
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from ..core.database import get_db
from ..models.agent import Agent, AgentBalance
from ..models.platform import MarketListing, MarketPurchase


async def _sync_market_balance(db, agent, token: str, delta: Decimal):
    bal = (await db.execute(
        select(AgentBalance).where(AgentBalance.agent_id == agent.id, AgentBalance.token == token).with_for_update()
    )).scalar_one_or_none()
    if not bal:
        bal = AgentBalance(agent_id=agent.id, token=token, balance=Decimal("0"), locked_balance=Decimal("0"))
        db.add(bal)
    bal.balance = Decimal(str(bal.balance)) + delta

router = APIRouter(prefix="/v1/market")

MARKET_COMMISSION = Decimal("0.05")  # 5%

CATEGORIES = ["dataset", "research", "model", "api_access", "data_feed", "tool", "other"]


class ListRequest(BaseModel):
    seller_agio_id: str
    title: str
    description: str
    category: str
    price: float
    price_token: str = "USDC"
    content_url: Optional[str] = None


@router.post("/list")
async def create_listing(req: ListRequest, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """Create a marketplace listing. Free to list."""
    from .auth_guard import verify_agent
    await verify_agent(req.seller_agio_id, authorization)
    if req.category not in CATEGORIES:
        raise HTTPException(400, f"Invalid category. Options: {CATEGORIES}")
    if req.price <= 0:
        raise HTTPException(400, "Price must be positive")

    listing = MarketListing(
        seller_agent=req.seller_agio_id,
        title=req.title[:200],
        description=req.description[:5000],
        category=req.category,
        price=Decimal(str(req.price)),
        price_token=req.price_token,
        content_url=req.content_url,
    )
    db.add(listing)
    await db.commit()
    await db.refresh(listing)

    return {"listing_id": listing.id, "title": listing.title, "price": float(listing.price), "status": "ACTIVE"}


@router.get("/search")
async def search_listings(
    category: str = Query(None),
    max_price: float = Query(1_000_000),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Search marketplace listings."""
    base_filter = select(MarketListing).where(MarketListing.status == "ACTIVE", MarketListing.price <= max_price)
    if category:
        base_filter = base_filter.where(MarketListing.category == category)
    total = (await db.execute(select(func.count()).select_from(base_filter.subquery()))).scalar() or 0
    query = base_filter.order_by(MarketListing.total_sales.desc()).offset((page - 1) * limit).limit(limit)
    listings = (await db.execute(query)).scalars().all()

    return {
        "total": total,
        "listings": [
            {
                "id": l.id, "title": l.title, "category": l.category,
                "price": float(l.price), "token": l.price_token,
                "seller": l.seller_agent[:20] + "...",
                "sales": l.total_sales, "rating": float(l.avg_rating),
            }
            for l in listings
        ],
    }


@router.post("/purchase/{listing_id}")
async def purchase(listing_id: int, authorization: str = Header(None), buyer_id: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Purchase a listing. Debits buyer, credits seller minus 5% commission."""
    from .auth_guard import verify_agent
    await verify_agent(buyer_id, authorization)
    listing = (await db.execute(select(MarketListing).where(MarketListing.id == listing_id))).scalar_one_or_none()
    if not listing or listing.status != "ACTIVE":
        raise HTTPException(404, "Listing not found or inactive")
    if listing.seller_agent == buyer_id:
        raise HTTPException(400, "Cannot buy your own listing")

    buyer = (await db.execute(select(Agent).where(Agent.agio_id == buyer_id).with_for_update())).scalar_one_or_none()
    if not buyer:
        raise HTTPException(404, "Buyer not found")

    buyer_bal = (await db.execute(
        select(AgentBalance).where(AgentBalance.agent_id == buyer.id, AgentBalance.token == listing.price_token).with_for_update()
    )).scalar_one_or_none()
    available = Decimal(str(buyer_bal.balance)) - Decimal(str(buyer_bal.locked_balance)) if buyer_bal else Decimal("0")
    if available < listing.price:
        raise HTTPException(400, f"Insufficient balance: ${float(available):.2f}")

    seller = (await db.execute(select(Agent).where(Agent.agio_id == listing.seller_agent).with_for_update())).scalar_one_or_none()
    if not seller:
        raise HTTPException(404, "Seller not found")

    commission = (listing.price * MARKET_COMMISSION).quantize(Decimal("0.000001"))
    seller_payout = listing.price - commission

    buyer.balance = Decimal(str(buyer.balance)) - listing.price
    await _sync_market_balance(db, buyer, listing.price_token, -listing.price)
    seller.balance = Decimal(str(seller.balance)) + seller_payout
    await _sync_market_balance(db, seller, listing.price_token, seller_payout)
    listing.total_sales += 1

    # Record marketplace commission as revenue
    try:
        from sqlalchemy import text
        await db.execute(text(
            "INSERT INTO platform_revenue (source, amount, token, reference_id, created_at) "
            "VALUES (:src, :amt, :tok, :ref, NOW())"
        ), {"src": "marketplace_commission", "amt": float(commission), "tok": listing.price_token, "ref": str(listing_id)})
    except Exception:
        try:
            await db.execute(text(
                "CREATE TABLE IF NOT EXISTS platform_revenue ("
                "id SERIAL PRIMARY KEY, source VARCHAR(30), amount NUMERIC(20,6), "
                "token VARCHAR(10), reference_id VARCHAR(66), created_at TIMESTAMP DEFAULT NOW())"
            ))
            await db.commit()
            await db.execute(text(
                "INSERT INTO platform_revenue (source, amount, token, reference_id, created_at) "
                "VALUES (:src, :amt, :tok, :ref, NOW())"
            ), {"src": "marketplace_commission", "amt": float(commission), "tok": listing.price_token, "ref": str(listing_id)})
        except Exception:
            pass

    purchase = MarketPurchase(listing_id=listing_id, buyer_agent=buyer_id)
    db.add(purchase)
    await db.commit()

    return {
        "purchase_id": purchase.id, "listing_id": listing_id,
        "price": float(listing.price), "commission": float(commission),
        "seller_received": float(seller_payout),
        "content_url": listing.content_url,
    }


@router.post("/rate/{listing_id}")
async def rate_purchase(listing_id: int, buyer_id: str = Query(...), rating: int = Query(..., ge=1, le=5), db: AsyncSession = Depends(get_db)):
    """Rate a purchase (1-5 stars)."""
    purchase = (await db.execute(
        select(MarketPurchase).where(MarketPurchase.listing_id == listing_id, MarketPurchase.buyer_agent == buyer_id)
    )).scalar_one_or_none()
    if not purchase:
        raise HTTPException(404, "Purchase not found")
    if purchase.rating:
        raise HTTPException(400, "Already rated")

    purchase.rating = rating

    # Update average rating
    listing = (await db.execute(select(MarketListing).where(MarketListing.id == listing_id))).scalar_one()
    all_ratings = (await db.execute(
        select(func.avg(MarketPurchase.rating)).where(MarketPurchase.listing_id == listing_id, MarketPurchase.rating.isnot(None))
    )).scalar()
    listing.avg_rating = Decimal(str(all_ratings or 0))
    await db.commit()

    return {"listing_id": listing_id, "rating": rating, "avg_rating": float(listing.avg_rating)}

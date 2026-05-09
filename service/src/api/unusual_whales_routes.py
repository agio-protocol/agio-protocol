# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Unusual Whales API — options flow, Congress trades, dark pool data."""
import os
from fastapi import APIRouter, Depends, Query, Header, HTTPException
import httpx

router = APIRouter(prefix="/v1/unusual-whales", tags=["unusual-whales"])

UW_KEY = os.getenv("UNUSUAL_WHALES_KEY", "")
UW_BASE = "https://api.unusualwhales.com/api"


async def _require_auth(authorization: str):
    if not authorization or not authorization.startswith("Bearer ses_"):
        raise HTTPException(401, "Sign in to access Unusual Whales data")
    token = authorization.replace("Bearer ", "")
    from ..core.redis import redis_client
    if not await redis_client.get(f"session:{token}"):
        raise HTTPException(401, "Session expired")


async def _uw_get(path: str, params: dict = None) -> dict:
    if not UW_KEY:
        return {"error": "Unusual Whales not configured"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{UW_BASE}{path}",
                                headers={"Authorization": f"Bearer {UW_KEY}"},
                                params=params or {}, timeout=15)
        if resp.status_code != 200:
            return {"error": f"UW API returned {resp.status_code}"}
        return resp.json()


@router.get("/options-flow")
async def options_flow(
    ticker: str = Query(None),
    limit: int = Query(30, ge=1, le=100),
    authorization: str = Header(None),
):
    """Real-time unusual options activity."""
    await _require_auth(authorization)
    data = await _uw_get("/option-trades/flow-alerts")
    alerts = data.get("data", [])

    if ticker:
        alerts = [a for a in alerts if a.get("ticker", "").upper() == ticker.upper()]

    results = []
    for a in alerts[:limit]:
        total_prem = float(a.get("total_premium", 0) or 0)
        bid_prem = float(a.get("total_bid_side_prem", 0) or 0)
        ask_prem = float(a.get("total_ask_side_prem", 0) or 0)
        sentiment = "BULLISH" if ask_prem > bid_prem * 1.5 else "BEARISH" if bid_prem > ask_prem * 1.5 else "MIXED"

        results.append({
            "ticker": a.get("ticker"),
            "type": a.get("type"),
            "strike": a.get("strike"),
            "expiry": a.get("expiry"),
            "total_premium": total_prem,
            "total_size": a.get("total_size"),
            "volume": a.get("volume"),
            "open_interest": a.get("open_interest"),
            "underlying_price": a.get("underlying_price"),
            "iv_start": a.get("iv_start"),
            "iv_end": a.get("iv_end"),
            "sentiment": sentiment,
            "has_sweep": a.get("has_sweep"),
            "has_floor": a.get("has_floor"),
            "sector": a.get("sector"),
            "alert_rule": a.get("alert_rule"),
            "created_at": a.get("created_at"),
        })

    return {"count": len(results), "flow": results}


@router.get("/congress")
async def congress_trades(
    ticker: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    authorization: str = Header(None),
):
    """Congress trading disclosures with full detail."""
    await _require_auth(authorization)
    data = await _uw_get("/congress/recent-trades")
    trades = data.get("data", [])

    if ticker:
        trades = [t for t in trades if t.get("ticker", "").upper() == ticker.upper()]

    results = []
    for t in trades[:limit]:
        results.append({
            "politician": t.get("name") or t.get("reporter"),
            "ticker": t.get("ticker"),
            "issuer": t.get("issuer"),
            "transaction_type": t.get("txn_type"),
            "amount": t.get("amounts"),
            "transaction_date": t.get("transaction_date"),
            "filed_date": t.get("filed_at_date"),
            "member_type": t.get("member_type"),
            "notes": t.get("notes"),
        })

    return {"count": len(results), "trades": results}


@router.get("/congress/politicians")
async def congress_politicians(authorization: str = Header(None)):
    """List all politicians with trade data."""
    await _require_auth(authorization)
    data = await _uw_get("/congress/politicians")
    return data


@router.get("/darkpool")
async def dark_pool(
    ticker: str = Query(None),
    limit: int = Query(30, ge=1, le=100),
    authorization: str = Header(None),
):
    """Recent dark pool trades."""
    await _require_auth(authorization)
    if ticker:
        data = await _uw_get(f"/darkpool/{ticker.upper()}")
    else:
        data = await _uw_get("/darkpool/recent")
    trades = data.get("data", [])

    results = []
    for t in trades[:limit]:
        results.append({
            "ticker": t.get("ticker"),
            "price": t.get("price"),
            "size": t.get("size"),
            "volume": t.get("volume"),
            "premium": t.get("premium"),
            "executed_at": t.get("executed_at") or t.get("tracking_timestamp"),
        })

    return {"count": len(results), "trades": results}


@router.get("/ticker/{ticker}")
async def ticker_overview(ticker: str, authorization: str = Header(None)):
    """Full Unusual Whales overview for a stock — options flow + dark pool + company profile."""
    await _require_auth(authorization)

    flow_data = await _uw_get("/option-trades/flow-alerts")
    dp_data = await _uw_get(f"/darkpool/{ticker.upper()}")
    profile_data = await _uw_get(f"/companies/{ticker.upper()}/profile")

    flow_alerts = [a for a in flow_data.get("data", []) if a.get("ticker", "").upper() == ticker.upper()]
    dp_trades = dp_data.get("data", [])[:10]
    profile = profile_data.get("data", profile_data)

    total_prem = sum(float(a.get("total_premium", 0) or 0) for a in flow_alerts)
    bullish_prem = sum(float(a.get("total_ask_side_prem", 0) or 0) for a in flow_alerts)
    bearish_prem = sum(float(a.get("total_bid_side_prem", 0) or 0) for a in flow_alerts)

    return {
        "ticker": ticker.upper(),
        "profile": profile if isinstance(profile, dict) else {},
        "options_flow": {
            "alert_count": len(flow_alerts),
            "total_premium": total_prem,
            "bullish_premium": bullish_prem,
            "bearish_premium": bearish_prem,
            "sentiment": "BULLISH" if bullish_prem > bearish_prem * 1.3 else "BEARISH" if bearish_prem > bullish_prem * 1.3 else "MIXED",
            "top_alerts": [{
                "type": a.get("type"), "strike": a.get("strike"), "expiry": a.get("expiry"),
                "premium": float(a.get("total_premium", 0) or 0), "volume": a.get("volume"),
            } for a in flow_alerts[:5]],
        },
        "dark_pool": {
            "recent_trades": len(dp_trades),
            "trades": [{
                "price": t.get("price"), "size": t.get("size"), "volume": t.get("volume"),
            } for t in dp_trades[:5]],
        },
    }

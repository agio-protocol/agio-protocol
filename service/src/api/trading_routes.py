# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Live Trading API — execute trades via Jupiter on Solana."""
import os
import json as _json
from fastapi import APIRouter, Depends, Query, Header, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from decimal import Decimal

from ..core.database import get_db

router = APIRouter(prefix="/v1/trading", tags=["trading"])


async def _require_admin(x_admin_key: str = Header(None)):
    admin_key = os.getenv("ADMIN_API_KEY", "")
    if not admin_key or x_admin_key != admin_key:
        raise HTTPException(401, "Admin access required")


async def _check_kill_switch():
    """Check if trading is paused."""
    try:
        from ..core.redis import redis_client
        paused = await redis_client.get("trading:paused")
        if paused == "1":
            raise HTTPException(503, "Trading is currently paused")
    except HTTPException:
        raise
    except:
        pass


@router.get("/wallet")
async def wallet_info(_=Depends(_require_admin)):
    """View trading wallet address and balances."""
    from ..services.jupiter_swap import get_balance
    try:
        return await get_balance()
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/config")
async def get_trading_config(_=Depends(_require_admin)):
    """View live trading configuration."""
    try:
        from ..core.redis import redis_client
        config = await redis_client.get("trading:config")
        if config:
            return _json.loads(config)
    except:
        pass
    return {
        "live_mode": False,
        "max_position_size_sol": 0.5,
        "max_daily_loss_sol": 2.0,
        "default_slippage_bps": 200,
        "sell_slippage_bps": 300,
        "priority_fee_lamports": 50000,
        "max_open_positions": 5,
        "allowed_traders": ["paper_trader"],  # which bots can go live
    }


@router.post("/config")
async def update_trading_config(request: Request, _=Depends(_require_admin)):
    """Update live trading configuration."""
    try:
        from ..core.redis import redis_client
        body = await request.json()
        # Get current config
        current = await redis_client.get("trading:config")
        config = _json.loads(current) if current else {
            "live_mode": False,
            "max_position_size_sol": 0.5,
            "max_daily_loss_sol": 2.0,
            "default_slippage_bps": 200,
            "sell_slippage_bps": 300,
            "priority_fee_lamports": 50000,
            "max_open_positions": 5,
            "allowed_traders": ["paper_trader"],
        }
        config.update(body)
        await redis_client.set("trading:config", _json.dumps(config))
        return {"status": "updated", "config": config}
    except Exception as e:
        raise HTTPException(500, f"Config update failed: {e}")


@router.post("/pause")
async def pause_trading(_=Depends(_require_admin)):
    """Emergency kill switch — pause all live trading."""
    try:
        from ..core.redis import redis_client
        await redis_client.set("trading:paused", "1")
        return {"status": "paused", "message": "All live trading is now PAUSED"}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/resume")
async def resume_trading(_=Depends(_require_admin)):
    """Resume live trading after a pause."""
    try:
        from ..core.redis import redis_client
        await redis_client.delete("trading:paused")
        return {"status": "resumed", "message": "Live trading is now ACTIVE"}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/status")
async def trading_status():
    """Public endpoint — is live trading active?"""
    paused = False
    live = False
    try:
        from ..core.redis import redis_client
        paused = (await redis_client.get("trading:paused")) == "1"
        config = await redis_client.get("trading:config")
        if config:
            live = _json.loads(config).get("live_mode", False)
    except:
        pass
    return {
        "live_mode": live,
        "paused": paused,
        "status": "PAUSED" if paused else ("LIVE" if live else "PAPER"),
    }


@router.post("/quote")
async def get_quote(request: Request, _=Depends(_require_admin)):
    """Get a Jupiter quote without executing.
    Body: {token_mint, amount_sol, direction: "buy"|"sell"}
    """
    from ..services.jupiter_swap import get_quote, SOL_MINT
    body = await request.json()
    token_mint = body.get("token_mint")
    amount_sol = float(body.get("amount_sol", 0))
    direction = body.get("direction", "buy")
    slippage = int(body.get("slippage_bps", 200))

    if not token_mint or amount_sol <= 0:
        raise HTTPException(400, "token_mint and amount_sol required")

    if direction == "buy":
        amount_lamports = int(amount_sol * 1e9)
        quote = await get_quote(SOL_MINT, token_mint, amount_lamports, slippage)
    else:
        # For sells, amount is in token base units — caller must provide
        amount_raw = int(body.get("amount_raw", 0))
        if amount_raw <= 0:
            raise HTTPException(400, "amount_raw required for sell quotes")
        quote = await get_quote(token_mint, SOL_MINT, amount_raw, slippage)

    if not quote:
        raise HTTPException(400, "Failed to get quote — token may have no liquidity")

    return {
        "in_amount": quote.get("inAmount"),
        "out_amount": quote.get("outAmount"),
        "price_impact": quote.get("priceImpactPct"),
        "routes": [r.get("swapInfo", {}).get("label", "?") for r in quote.get("routePlan", [])],
        "slippage_bps": slippage,
    }


@router.post("/execute")
async def execute_trade(request: Request, _=Depends(_require_admin)):
    """Execute a live trade via Jupiter.
    Body: {token_mint, amount_sol, direction: "buy"|"sell", slippage_bps?, priority_fee?}
    """
    await _check_kill_switch()

    # Check live mode
    try:
        from ..core.redis import redis_client
        config = await redis_client.get("trading:config")
        if config:
            tc = _json.loads(config)
            if not tc.get("live_mode", False):
                raise HTTPException(403, "Live trading is not enabled. Set live_mode=true in config.")
            max_size = tc.get("max_position_size_sol", 0.5)
        else:
            raise HTTPException(403, "Trading config not set. Configure via POST /v1/trading/config first.")
    except HTTPException:
        raise
    except:
        raise HTTPException(500, "Could not check trading config")

    from ..services.jupiter_swap import buy_token, sell_token
    body = await request.json()
    token_mint = body.get("token_mint")
    amount_sol = float(body.get("amount_sol", 0))
    direction = body.get("direction", "buy")
    slippage = int(body.get("slippage_bps", tc.get("default_slippage_bps", 200)))
    priority = int(body.get("priority_fee", tc.get("priority_fee_lamports", 50000)))

    if not token_mint:
        raise HTTPException(400, "token_mint required")

    if direction == "buy":
        if amount_sol <= 0 or amount_sol > max_size:
            raise HTTPException(400, f"amount_sol must be between 0 and {max_size}")

        # Check daily loss limit
        try:
            daily_loss = float(await redis_client.get("trading:daily_loss") or 0)
            max_daily = tc.get("max_daily_loss_sol", 2.0)
            if daily_loss >= max_daily:
                raise HTTPException(403, f"Daily loss limit reached (${daily_loss:.4f} SOL / ${max_daily} max)")
        except HTTPException:
            raise
        except:
            pass

        result = await buy_token(token_mint, amount_sol, slippage, priority)
    else:
        amount_raw = int(body.get("amount_raw", 0))
        if amount_raw <= 0:
            raise HTTPException(400, "amount_raw required for sells")
        token_decimals = int(body.get("token_decimals", 6))
        sell_slip = int(body.get("slippage_bps", tc.get("sell_slippage_bps", 300)))
        result = await sell_token(token_mint, amount_raw, token_decimals, sell_slip, priority)

    # Log the trade
    try:
        trade_log = {
            "direction": direction,
            "token_mint": token_mint,
            "amount_sol": amount_sol,
            "result": result,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await redis_client.rpush("trading:history", _json.dumps(trade_log, default=str))
        await redis_client.ltrim("trading:history", -1000, -1)  # Keep last 1000
    except:
        pass

    return result


@router.get("/history")
async def trade_history(limit: int = Query(20, ge=1, le=100), _=Depends(_require_admin)):
    """View recent live trade execution history."""
    try:
        from ..core.redis import redis_client
        history = await redis_client.lrange("trading:history", -limit, -1)
        trades = [_json.loads(h) for h in reversed(history)]
        return {"count": len(trades), "trades": trades}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/token-balance/{token_mint}")
async def token_balance(token_mint: str, _=Depends(_require_admin)):
    """Check balance of a specific token in the trading wallet."""
    from ..services.jupiter_swap import get_token_balance
    raw, ui, decimals = await get_token_balance(token_mint)
    return {"mint": token_mint, "raw_amount": raw, "ui_amount": ui, "decimals": decimals}

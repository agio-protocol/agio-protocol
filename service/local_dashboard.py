#!/usr/bin/env python3
# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Local monitoring dashboard for the 3 trading bots.
Run: python local_dashboard.py
Access: http://localhost:8080
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

# ── Load .env.local ────────────────────────────────────────────────────────────
_env_file = Path(__file__).parent / ".env.local"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_log = logging.getLogger("dashboard")

# ── Connection config ──────────────────────────────────────────────────────────
_DB_URL_RAW = os.environ.get(
    "DATABASE_URL",
    "postgresql://agio:agio_dev_password@localhost:5432/agio",
)
# asyncpg needs no driver prefix
_PG_DSN = _DB_URL_RAW.replace("postgresql+asyncpg://", "postgresql://")
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
_TRADING_WALLET_KEY = os.environ.get("TRADING_WALLET_PRIVATE_KEY", "")

# ── Global connection pool ─────────────────────────────────────────────────────
_pg_pool: asyncpg.Pool | None = None
_redis: aioredis.Redis | None = None

app = FastAPI(title="Agiotage Bot Dashboard", docs_url=None, redoc_url=None)


# ── Startup / shutdown ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    global _pg_pool, _redis
    try:
        _pg_pool = await asyncpg.create_pool(_PG_DSN, min_size=2, max_size=5, command_timeout=10)
        _log.info("PostgreSQL pool connected")
    except Exception as e:
        _log.warning(f"PostgreSQL connection failed (dashboard will still load): {e}")

    try:
        _redis = aioredis.from_url(_REDIS_URL, decode_responses=True)
        await _redis.ping()
        _log.info("Redis connected")
    except Exception as e:
        _log.warning(f"Redis connection failed (config reads will use defaults): {e}")


@app.on_event("shutdown")
async def _shutdown():
    if _pg_pool:
        await _pg_pool.close()
    if _redis:
        await _redis.aclose()


# ── Helper: get wallet public key ──────────────────────────────────────────────
def _resolve_wallet_pubkey(env_key: str) -> str | None:
    pk = os.environ.get(env_key, "")
    if not pk:
        return None
    try:
        from solders.keypair import Keypair
        if pk.startswith("["):
            return str(Keypair.from_bytes(bytes(json.loads(pk))).pubkey())
        import base58 as b58
        return str(Keypair.from_bytes(b58.b58decode(pk)).pubkey())
    except Exception:
        return None


# ── Data fetchers ──────────────────────────────────────────────────────────────
async def _fetch_sol_balance(pubkey: str) -> float:
    if not pubkey:
        return 0.0
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(_SOLANA_RPC, json={
                "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey],
            })
            if r.status_code == 200:
                return r.json().get("result", {}).get("value", 0) / 1e9
    except Exception:
        pass
    return 0.0


async def _fetch_token_price(token_address: str) -> tuple[float, float]:
    """Returns (price_usd, fdv). Cached per call — caller batches."""
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"https://api.dexscreener.com/token-pairs/v1/solana/{token_address}")
            if r.status_code == 200:
                data = r.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if pairs:
                    p = pairs[0]
                    return float(p.get("priceUsd", 0) or 0), float(p.get("fdv", 0) or 0)
    except Exception:
        pass
    return 0.0, 0.0


async def _get_redis_config(key: str, defaults: dict) -> dict:
    if not _redis:
        return defaults.copy()
    try:
        stored = await _redis.get(key)
        if stored:
            return {**defaults, **json.loads(stored)}
    except Exception:
        pass
    return defaults.copy()


async def _set_redis_config(key: str, updates: dict, defaults: dict) -> dict:
    current = await _get_redis_config(key, defaults)
    current.update(updates)
    if _redis:
        await _redis.set(key, json.dumps(current))
    return current


# ── Meme bot defaults (from paper_trader.py) ──────────────────────────────────
MEME_DEFAULTS = {
    "min_agiotage_score": 20,
    "min_mc": 250000,
    "max_mc": 10000000,
    "min_sources": 2,
    "min_wallet_count": 3,
    "base_position_sol": 0.20,
    "max_open_positions": 8,
    "daily_loss_limit_sol": 2.0,
    "stop_loss_pct": 35,
    "trailing_stop_enabled": True,
    "trailing_stop_activation_pct": 35,
    "trailing_stop_trail_pct": 30,
    "ratchet_stop_enabled": True,
    "ratchet_stop_pct_of_tp": 45,
    "max_holding_hours": 8,
    "min_volume_h1": 10000,
    "buy_slippage_bps": 200,
    "sell_slippage_bps": 500,
    "priority_fee_lamports": 50000,
    "take_profit_levels": [
        {"sell_pct": 45, "at_profit_pct": 25},
        {"sell_pct": 20, "at_profit_pct": 50},
        {"sell_pct": 20, "at_profit_pct": 100},
        {"sell_pct": 15, "at_profit_pct": 200},
    ],
}

COPY_DEFAULTS = {
    "enabled": True,
    "position_size_sol": 0.10,
    "max_open_positions": 5,
    "daily_loss_limit_sol": 0.50,
    "paper_mode": True,
    "min_wallet_winrate": 0.65,
    "stop_loss_pct": 25,
    "take_profit_pct": 50,
    "max_hold_hours": 6,
}

SNIPER_DEFAULTS = {
    "enabled": True,
    "paper_mode": True,
    "position_size_sol": 0.05,
    "max_open_positions": 5,
    "daily_loss_limit_sol": 0.50,
    "min_curve_pct": 25,
    "max_curve_pct": 50,
    "stop_loss_pct": 30,
    "tp1_pct": 50,
    "tp2_pct": 100,
}


# ── DB query helpers ───────────────────────────────────────────────────────────
async def _query(sql: str, *args) -> list[dict]:
    if not _pg_pool:
        return []
    try:
        async with _pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
            return [dict(r) for r in rows]
    except Exception as e:
        _log.warning(f"DB query error: {e}")
        return []


async def _query_one(sql: str, *args) -> dict | None:
    rows = await _query(sql, *args)
    return rows[0] if rows else None


# ── Status aggregator ──────────────────────────────────────────────────────────
async def _build_status() -> dict:
    now = datetime.utcnow()

    # ── Wallet balances ────────────────────────────────────────────────────────
    meme_pubkey = _resolve_wallet_pubkey("TRADING_WALLET_PRIVATE_KEY")
    copy_pubkey = _resolve_wallet_pubkey("COPY_TRADER_PRIVATE_KEY")
    sniper_pubkey = _resolve_wallet_pubkey("SNIPER_WALLET_PRIVATE_KEY")

    meme_bal, copy_bal, sniper_bal = await asyncio.gather(
        _fetch_sol_balance(meme_pubkey or ""),
        _fetch_sol_balance(copy_pubkey or ""),
        _fetch_sol_balance(sniper_pubkey or ""),
        return_exceptions=True,
    )
    meme_bal = meme_bal if isinstance(meme_bal, float) else 0.0
    copy_bal = copy_bal if isinstance(copy_bal, float) else 0.0
    sniper_bal = sniper_bal if isinstance(sniper_bal, float) else 0.0

    # ── Meme bot stats ─────────────────────────────────────────────────────────
    meme_stats = await _query_one("""
        SELECT
            COUNT(*) FILTER (WHERE action = 'SELL' OR action LIKE 'SELL%') AS total_sells,
            COUNT(*) FILTER (WHERE pnl_pct > 0 AND (action = 'SELL' OR action LIKE 'SELL%')) AS winners,
            COUNT(*) FILTER (WHERE pnl_pct <= 0 AND (action = 'SELL' OR action LIKE 'SELL%')) AS losers,
            COALESCE(SUM(CASE WHEN action LIKE 'SELL%' THEN usd_value ELSE 0 END), 0) AS total_pnl_usd
        FROM paper_trades
    """)
    meme_open_positions = await _query("""
        SELECT
            pp.id, pp.token_symbol, pp.token_address,
            pp.entry_price, pp.current_price, pp.entry_mc, pp.current_mc,
            pp.position_size_usd, pp.pnl_pct, pp.remaining_pct,
            pp.opened_at, pp.status, pp.agiotage_score, pp.close_reason,
            pp.trailing_active, pp.tier_1_done, pp.tier_2_done, pp.tier_3_done,
            pp.stop_price
        FROM paper_positions pp
        WHERE pp.status = 'OPEN'
        ORDER BY pp.opened_at DESC
    """)

    # ── Copy bot stats ─────────────────────────────────────────────────────────
    copy_config = await _get_redis_config("copy_trader_config", COPY_DEFAULTS)
    copy_stats = await _query_one("""
        SELECT
            COUNT(*) FILTER (WHERE action = 'SELL') AS total_sells,
            COUNT(*) FILTER (WHERE action = 'BUY') AS total_buys
        FROM copy_trades
    """)
    copy_wallets_count = await _query_one(
        "SELECT COUNT(*) AS cnt FROM copy_tracked_wallets WHERE active = TRUE"
    )
    copy_open_positions = await _query("""
        SELECT
            cp.id, cp.token_symbol, cp.token_address,
            cp.entry_price, cp.current_price, cp.position_size_sol, cp.position_size_usd,
            cp.pnl_pct, cp.wallet_label, cp.copied_wallet,
            cp.opened_at, cp.status, cp.close_reason
        FROM copy_positions cp
        WHERE cp.status = 'OPEN'
        ORDER BY cp.opened_at DESC
    """)

    # ── Sniper bot stats ───────────────────────────────────────────────────────
    sniper_config = await _get_redis_config("pumpfun_sniper_config", SNIPER_DEFAULTS)
    sniper_stats = await _query_one("""
        SELECT COUNT(*) AS total_trades FROM snipe_positions
    """)
    sniper_open = await _query("""
        SELECT
            sp.id, sp.symbol, sp.mint, sp.entry_price_sol,
            sp.current_price_sol, sp.position_size_sol,
            sp.pnl_pct, sp.entry_curve_pct, sp.graduated,
            sp.opened_at, sp.status, sp.close_reason
        FROM snipe_positions sp
        WHERE sp.status = 'OPEN'
        ORDER BY sp.opened_at DESC
    """)

    # ── Recent trades (last 20) ────────────────────────────────────────────────
    recent_meme = await _query("""
        SELECT
            pt.executed_at AS ts,
            pt.action,
            pp.token_symbol AS token,
            pp.token_address,
            pt.pnl_pct,
            pt.reason,
            'MEME' AS bot
        FROM paper_trades pt
        JOIN paper_positions pp ON pt.position_id = pp.id
        ORDER BY pt.executed_at DESC
        LIMIT 10
    """)
    recent_copy = await _query("""
        SELECT
            ct.created_at AS ts,
            ct.action,
            cp.token_symbol AS token,
            cp.token_address,
            ct.pnl_pct,
            ct.reason,
            'COPY' AS bot
        FROM copy_trades ct
        JOIN copy_positions cp ON ct.position_id = cp.id
        ORDER BY ct.created_at DESC
        LIMIT 10
    """)
    recent_snipe = await _query("""
        SELECT
            st.created_at AS ts,
            st.action,
            sp.symbol AS token,
            sp.mint AS token_address,
            st.pnl_pct,
            st.reason,
            'SNIPER' AS bot
        FROM snipe_trades st
        JOIN snipe_positions sp ON st.position_id = sp.id
        ORDER BY st.created_at DESC
        LIMIT 10
    """)

    # Merge + sort
    all_trades = recent_meme + recent_copy + recent_snipe
    all_trades.sort(key=lambda x: x.get("ts") or datetime.min, reverse=True)
    all_trades = all_trades[:20]

    # ── Meme config ────────────────────────────────────────────────────────────
    meme_config = await _get_redis_config("paper_trader_config", MEME_DEFAULTS)

    # ── Daily loss from Redis ──────────────────────────────────────────────────
    daily_loss_key = f"paper_trader:daily_loss:{now.strftime('%Y-%m-%d')}"
    daily_loss_sol = 0.0
    if _redis:
        try:
            v = await _redis.get(daily_loss_key)
            daily_loss_sol = float(v or 0)
        except Exception:
            pass

    # ── Compute meme win rate ──────────────────────────────────────────────────
    total_sells = int((meme_stats or {}).get("total_sells") or 0)
    winners = int((meme_stats or {}).get("winners") or 0)
    losers = int((meme_stats or {}).get("losers") or 0)
    win_rate = (winners / total_sells * 100) if total_sells > 0 else 0.0
    total_pnl_usd = float((meme_stats or {}).get("total_pnl_usd") or 0)

    # ── Enrich open positions with live prices (batch, up to 8) ───────────────
    price_tasks = {}
    for pos in meme_open_positions[:8]:
        addr = pos.get("token_address", "")
        if addr and addr not in price_tasks:
            price_tasks[addr] = _fetch_token_price(addr)

    if price_tasks:
        price_results = dict(zip(
            price_tasks.keys(),
            await asyncio.gather(*price_tasks.values(), return_exceptions=True),
        ))
    else:
        price_results = {}

    for pos in meme_open_positions:
        addr = pos.get("token_address", "")
        result = price_results.get(addr)
        if isinstance(result, tuple):
            live_price, live_mc = result
            if live_price > 0:
                entry = float(pos.get("entry_price") or 0)
                pos["live_price"] = live_price
                pos["live_mc"] = live_mc
                pos["live_pnl_pct"] = ((live_price - entry) / entry * 100) if entry > 0 else 0
            else:
                pos["live_price"] = float(pos.get("current_price") or 0)
                pos["live_mc"] = float(pos.get("current_mc") or 0)
                pos["live_pnl_pct"] = float(pos.get("pnl_pct") or 0)
        else:
            pos["live_price"] = float(pos.get("current_price") or 0)
            pos["live_mc"] = float(pos.get("current_mc") or 0)
            pos["live_pnl_pct"] = float(pos.get("pnl_pct") or 0)

    # Serialise datetimes
    def _ser(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    def _clean(lst):
        return [{k: _ser(v) for k, v in row.items()} for row in lst]

    return {
        "ts": now.isoformat(),
        "db_connected": _pg_pool is not None,
        "redis_connected": _redis is not None,
        "meme": {
            "wallet_pubkey": meme_pubkey,
            "wallet_sol": meme_bal,
            "total_trades": total_sells,
            "winners": winners,
            "losers": losers,
            "win_rate_pct": round(win_rate, 1),
            "total_pnl_usd": round(total_pnl_usd, 2),
            "daily_loss_sol": round(daily_loss_sol, 4),
            "open_positions": _clean(meme_open_positions),
            "config": meme_config,
            "paused": meme_config.get("daily_loss_limit_sol", 2.0) < 0.01,
        },
        "copy": {
            "wallet_pubkey": copy_pubkey,
            "wallet_sol": copy_bal,
            "mode": "paper" if copy_config.get("paper_mode", True) else "live",
            "tracked_wallets": int((copy_wallets_count or {}).get("cnt") or 0),
            "total_trades": int((copy_stats or {}).get("total_buys") or 0),
            "open_positions": _clean(copy_open_positions),
            "config": copy_config,
        },
        "sniper": {
            "wallet_pubkey": sniper_pubkey,
            "wallet_sol": sniper_bal,
            "mode": "paper" if sniper_config.get("paper_mode", True) else "live",
            "total_trades": int((sniper_stats or {}).get("total_trades") or 0),
            "open_positions": _clean(sniper_open),
            "config": sniper_config,
        },
        "recent_trades": _clean(all_trades),
    }


# ── API endpoints ──────────────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    """JSON status — all bot stats, positions, balances."""
    try:
        return await _build_status()
    except Exception as e:
        _log.error(f"Status error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/config")
async def update_config(request: Request):
    """Update meme bot config. Body: JSON dict of fields to update."""
    try:
        updates = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    allowed = {
        "base_position_sol", "position_sol_score_45", "position_sol_score_55",
        "position_sol_score_65", "max_open_positions", "daily_loss_limit_sol",
        "stop_loss_pct", "trailing_stop_enabled", "trailing_stop_activation_pct",
        "trailing_stop_trail_pct", "ratchet_stop_enabled", "ratchet_stop_pct_of_tp",
        "max_holding_hours", "min_mc", "max_mc", "min_agiotage_score",
        "min_sources", "min_wallet_count", "buy_slippage_bps", "sell_slippage_bps",
        "priority_fee_lamports", "min_volume_h1", "take_profit_levels",
        "breakeven_stop_after_first_tp",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    new_cfg = await _set_redis_config("paper_trader_config", filtered, MEME_DEFAULTS)
    return {"status": "ok", "updated": filtered, "config": new_cfg}


@app.post("/config/copy")
async def update_copy_config(request: Request):
    """Update copy bot config."""
    try:
        updates = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    allowed = {
        "position_size_sol", "max_open_positions", "daily_loss_limit_sol",
        "stop_loss_pct", "take_profit_pct", "max_hold_hours", "min_wallet_winrate",
        "paper_mode", "min_mc", "max_mc", "min_volume_h1", "min_buy_usd",
        "cooldown_minutes", "copy_sell", "max_tracked_wallets", "enabled",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields")
    new_cfg = await _set_redis_config("copy_trader_config", filtered, COPY_DEFAULTS)
    return {"status": "ok", "updated": filtered, "config": new_cfg}


@app.post("/config/sniper")
async def update_sniper_config(request: Request):
    """Update sniper bot config."""
    try:
        updates = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    allowed = {
        "position_size_sol", "max_open_positions", "daily_loss_limit_sol",
        "stop_loss_pct", "tp1_pct", "tp1_sell_pct", "tp2_pct", "tp2_sell_pct",
        "graduation_sell_pct", "max_hold_minutes", "min_curve_pct", "max_curve_pct",
        "min_holders", "min_volume_sol", "max_dev_pct", "paper_mode", "enabled",
        "slippage_pct", "max_token_age_minutes",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields")
    new_cfg = await _set_redis_config("pumpfun_sniper_config", filtered, SNIPER_DEFAULTS)
    return {"status": "ok", "updated": filtered, "config": new_cfg}


@app.post("/control/pause")
async def pause_meme_bot():
    """Pause the meme bot by setting daily_loss_limit to near-zero."""
    new_cfg = await _set_redis_config(
        "paper_trader_config", {"daily_loss_limit_sol": 0.001}, MEME_DEFAULTS
    )
    return {"status": "paused", "daily_loss_limit_sol": new_cfg["daily_loss_limit_sol"]}


@app.post("/control/unpause")
async def unpause_meme_bot():
    """Unpause the meme bot by restoring default daily loss limit."""
    new_cfg = await _set_redis_config(
        "paper_trader_config", {"daily_loss_limit_sol": 2.0}, MEME_DEFAULTS
    )
    return {"status": "unpaused", "daily_loss_limit_sol": new_cfg["daily_loss_limit_sol"]}


@app.post("/control/sell-stuck/meme")
async def sell_stuck_meme():
    """Sell all stuck meme bot tokens on-chain via Jupiter."""
    return await _sell_stuck_tokens("meme")


@app.post("/control/sell-stuck/copy")
async def sell_stuck_copy():
    """Sell all stuck copy bot tokens on-chain via Jupiter."""
    return await _sell_stuck_tokens("copy")


@app.post("/control/sell-stuck/sniper")
async def sell_stuck_sniper():
    """Sell all stuck sniper bot tokens on-chain via Jupiter."""
    return await _sell_stuck_tokens("sniper")


async def _sell_stuck_tokens(bot: str):
    """Actually sell tokens on-chain and close DB positions."""
    if not _TRADING_WALLET_KEY:
        raise HTTPException(status_code=503, detail="No trading wallet key configured")

    # Get wallet address
    try:
        from solders.keypair import Keypair
        import base58 as b58
        pk = _TRADING_WALLET_KEY
        if pk.startswith("["):
            kp = Keypair.from_bytes(bytes(json.loads(pk)))
        else:
            kp = Keypair.from_bytes(b58.b58decode(pk))
        wallet_addr = str(kp.pubkey())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Wallet key error: {e}")

    # Get all tokens held on-chain (both SPL + Token-2022)
    sold = []
    failed = []
    async with httpx.AsyncClient() as client:
        for program_id in [
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        ]:
            try:
                resp = await client.post(_SOLANA_RPC, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [wallet_addr, {"programId": program_id},
                               {"encoding": "jsonParsed"}],
                }, timeout=15)
                if resp.status_code == 200:
                    accounts = resp.json().get("result", {}).get("value", [])
                    for acc in accounts:
                        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                        raw = int(info.get("tokenAmount", {}).get("amount", 0) or 0)
                        mint = info.get("mint", "")
                        decimals = int(info.get("tokenAmount", {}).get("decimals", 6))
                        ui_amount = float(info.get("tokenAmount", {}).get("uiAmount", 0) or 0)
                        if raw > 0 and mint:
                            # Sell via Jupiter
                            try:
                                quote_resp = await client.get(
                                    "https://api.jup.ag/swap/v1/quote",
                                    params={
                                        "inputMint": mint,
                                        "outputMint": "So11111111111111111111111111111111111111112",
                                        "amount": str(raw),
                                        "slippageBps": 800,
                                    }, timeout=10)
                                if quote_resp.status_code != 200:
                                    failed.append({"mint": mint[:20], "error": "quote failed"})
                                    continue

                                quote = quote_resp.json()
                                swap_resp = await client.post(
                                    "https://api.jup.ag/swap/v1/swap",
                                    json={
                                        "quoteResponse": quote,
                                        "userPublicKey": wallet_addr,
                                        "wrapAndUnwrapSol": True,
                                        "computeUnitPriceMicroLamports": 50000,
                                        "dynamicComputeUnitLimit": True,
                                    }, timeout=15)
                                if swap_resp.status_code != 200:
                                    failed.append({"mint": mint[:20], "error": "swap failed"})
                                    continue

                                swap_tx = swap_resp.json().get("swapTransaction")
                                if not swap_tx:
                                    failed.append({"mint": mint[:20], "error": "no tx"})
                                    continue

                                import base64
                                from solders.transaction import VersionedTransaction
                                tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx))
                                signed_tx = VersionedTransaction(tx.message, [kp])

                                send_resp = await client.post(_SOLANA_RPC, json={
                                    "jsonrpc": "2.0", "id": 1,
                                    "method": "sendTransaction",
                                    "params": [base64.b64encode(bytes(signed_tx)).decode(),
                                               {"encoding": "base64", "skipPreflight": False}],
                                }, timeout=30)

                                if send_resp.status_code == 200 and "result" in send_resp.json():
                                    tx_hash = send_resp.json()["result"]
                                    sold.append({"mint": mint[:20], "tx": tx_hash[:20], "amount": ui_amount})
                                else:
                                    failed.append({"mint": mint[:20], "error": "send failed"})

                                await asyncio.sleep(2)
                            except Exception as e:
                                failed.append({"mint": mint[:20], "error": str(e)[:50]})
            except Exception as e:
                _log.error(f"Token scan error: {e}")

    # Close DB positions for the bot
    table = {"meme": "paper_positions", "copy": "copy_positions", "sniper": "snipe_positions"}.get(bot)
    closed_db = 0
    if table and _pg_pool:
        try:
            async with _pg_pool.acquire() as conn:
                result = await conn.execute(f"""
                    UPDATE {table}
                    SET status = 'CLOSED', close_reason = 'Dashboard: force sell'
                    WHERE status = 'OPEN'
                """)
                closed_db = int(result.split()[-1]) if result else 0
        except Exception:
            pass

    return {
        "status": "ok",
        "bot": bot,
        "tokens_sold": len(sold),
        "tokens_failed": len(failed),
        "db_positions_closed": closed_db,
        "sold": sold,
        "failed": failed,
    }


# ── HTML Dashboard ─────────────────────────────────────────────────────────────
_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agiotage Bot Dashboard</title>
<style>
  :root {
    --bg: #0d0d0f;
    --surface: #16161a;
    --surface2: #1e1e24;
    --border: #2a2a35;
    --text: #e2e2e8;
    --muted: #7a7a90;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #eab308;
    --blue: #3b82f6;
    --purple: #a855f7;
    --accent: #6366f1;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 13px;
    line-height: 1.5;
  }
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 12px 20px;
    display: flex;
    align-items: center;
    gap: 16px;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  header h1 { font-size: 16px; font-weight: 700; color: var(--accent); letter-spacing: 0.05em; }
  .header-meta { margin-left: auto; display: flex; gap: 12px; align-items: center; }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .badge-green { background: rgba(34,197,94,.15); color: var(--green); border: 1px solid rgba(34,197,94,.3); }
  .badge-red { background: rgba(239,68,68,.15); color: var(--red); border: 1px solid rgba(239,68,68,.3); }
  .badge-yellow { background: rgba(234,179,8,.15); color: var(--yellow); border: 1px solid rgba(234,179,8,.3); }
  .badge-blue { background: rgba(59,130,246,.15); color: var(--blue); border: 1px solid rgba(59,130,246,.3); }
  .badge-purple { background: rgba(168,85,247,.15); color: var(--purple); border: 1px solid rgba(168,85,247,.3); }
  .badge-muted { background: rgba(122,122,144,.15); color: var(--muted); border: 1px solid rgba(122,122,144,.3); }
  #refresh-timer { color: var(--muted); font-size: 11px; }
  main { padding: 16px 20px; max-width: 1600px; margin: 0 auto; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 12px; }
  .grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 12px; }
  @media (max-width: 1100px) { .grid-3 { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 700px) { .grid-3, .grid-2 { grid-template-columns: 1fr; } }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  .card-header {
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
    padding: 10px 14px;
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 700;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  .card-body { padding: 14px; }
  .stat-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 5px 0;
    border-bottom: 1px solid rgba(255,255,255,.04);
  }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: var(--muted); }
  .stat-value { font-weight: 600; }
  .green { color: var(--green); }
  .red { color: var(--red); }
  .yellow { color: var(--yellow); }
  .blue { color: var(--blue); }
  .muted { color: var(--muted); }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  thead th {
    text-align: left;
    padding: 8px 10px;
    color: var(--muted);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border-bottom: 1px solid var(--border);
    background: var(--surface2);
    font-size: 11px;
  }
  tbody tr {
    border-bottom: 1px solid rgba(255,255,255,.03);
    transition: background .1s;
  }
  tbody tr:hover { background: rgba(255,255,255,.03); }
  tbody tr:last-child { border-bottom: none; }
  td { padding: 7px 10px; vertical-align: middle; }
  .actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  button {
    padding: 7px 14px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--text);
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all .15s;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  button:hover { border-color: var(--accent); color: var(--accent); }
  button.danger:hover { border-color: var(--red); color: var(--red); }
  button.success:hover { border-color: var(--green); color: var(--green); }
  #status-bar {
    position: fixed;
    bottom: 12px;
    right: 16px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 11px;
    color: var(--muted);
    opacity: 0;
    transition: opacity .3s;
    z-index: 200;
  }
  #status-bar.show { opacity: 1; }
  .pos-row-green { background: rgba(34,197,94,.04) !important; }
  .pos-row-red { background: rgba(239,68,68,.04) !important; }
  .link { color: var(--accent); text-decoration: none; font-size: 11px; }
  .link:hover { text-decoration: underline; }
  .empty { color: var(--muted); padding: 14px; text-align: center; font-style: italic; }
  .config-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 6px; }
  .cfg-item { background: var(--surface2); border-radius: 4px; padding: 6px 10px; }
  .cfg-key { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 2px; }
  .cfg-val { font-weight: 600; font-size: 12px; }
  .section-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: var(--muted);
    margin: 16px 0 8px;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--border);
  }
  .wallet-addr { font-size: 10px; color: var(--muted); font-family: monospace; }
</style>
</head>
<body>
<header>
  <h1>&#9670; AGIOTAGE BOTS</h1>
  <span id="db-badge" class="badge badge-muted">DB ...</span>
  <span id="redis-badge" class="badge badge-muted">REDIS ...</span>
  <div class="header-meta">
    <span id="refresh-timer">Refreshing in 10s</span>
    <span id="last-update" class="muted" style="font-size:11px"></span>
  </div>
</header>

<main>
  <!-- Bot control buttons -->
  <div class="actions">
    <button onclick="pauseBot()" class="danger" title="Sets daily_loss_limit to 0.001 SOL">&#9646;&#9646; PAUSE MEME BOT</button>
    <button onclick="unpauseBot()" class="success" title="Restores daily_loss_limit to 2.0 SOL">&#9654; UNPAUSE MEME BOT</button>
    <button onclick="sellStuck('meme')" class="danger" title="Sell all stuck meme tokens on-chain">&#9888; Sell Meme Tokens</button>
    <button onclick="sellStuck('copy')" class="danger" title="Sell all stuck copy tokens on-chain">&#9888; Sell Copy Tokens</button>
    <button onclick="sellStuck('sniper')" class="danger" title="Sell all stuck sniper tokens on-chain">&#9888; Sell Sniper Tokens</button>
    <button onclick="refreshNow()">&#8635; REFRESH NOW</button>
  </div>

  <!-- Bot summary cards -->
  <div class="grid-3" id="bot-cards">
    <div class="card" id="meme-card">
      <div class="card-header">&#127918; Meme Bot</div>
      <div class="card-body" id="meme-body">Loading...</div>
    </div>
    <div class="card" id="copy-card">
      <div class="card-header">&#128203; Copy Bot</div>
      <div class="card-body" id="copy-body">Loading...</div>
    </div>
    <div class="card" id="sniper-card">
      <div class="card-header">&#127919; Sniper Bot</div>
      <div class="card-body" id="sniper-body">Loading...</div>
    </div>
  </div>

  <!-- Open positions -->
  <div class="grid-2">
    <div class="card">
      <div class="card-header">&#128200; Meme Open Positions</div>
      <div id="meme-positions">Loading...</div>
    </div>
    <div class="card">
      <div class="card-header">&#128200; Copy / Sniper Positions</div>
      <div id="other-positions">Loading...</div>
    </div>
  </div>

  <!-- Recent trades -->
  <div class="card" style="margin-bottom:12px">
    <div class="card-header">&#128337; Recent Trades (last 20)</div>
    <div id="recent-trades">Loading...</div>
  </div>

  <!-- Config -->
  <div class="grid-2">
    <div class="card">
      <div class="card-header">&#9881; Meme Bot Config</div>
      <div class="card-body" id="meme-config">Loading...</div>
    </div>
    <div class="card">
      <div class="card-header">&#9881; Copy &amp; Sniper Config</div>
      <div class="card-body" id="other-config">Loading...</div>
    </div>
  </div>
</main>

<div id="status-bar"></div>

<script>
let _data = null;
let _countdown = 10;
let _timer = null;

function fmt(n, digits=2) {
  if (n === null || n === undefined) return '—';
  return Number(n).toFixed(digits);
}
function fmtSol(n) { return fmt(n, 4) + ' SOL'; }
function fmtUsd(n) { return '$' + fmt(n, 2); }
function fmtPnl(p) {
  if (p === null || p === undefined) return '<span class="muted">—</span>';
  const v = Number(p);
  const cls = v > 0 ? 'green' : v < 0 ? 'red' : 'muted';
  return `<span class="${cls}">${v > 0 ? '+' : ''}${fmt(v, 1)}%</span>`;
}
function fmtMc(n) {
  if (!n) return '—';
  const v = Number(n);
  if (v >= 1e6) return '$' + fmt(v/1e6, 2) + 'M';
  if (v >= 1e3) return '$' + fmt(v/1e3, 1) + 'K';
  return '$' + fmt(v, 0);
}
function timeSince(iso) {
  if (!iso) return '—';
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
  const s = Math.floor((Date.now() - d) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  return Math.floor(s/3600) + 'h ago';
}
function badgeBot(bot) {
  if (bot === 'MEME') return '<span class="badge badge-purple">MEME</span>';
  if (bot === 'COPY') return '<span class="badge badge-blue">COPY</span>';
  if (bot === 'SNIPER') return '<span class="badge badge-yellow">SNIPE</span>';
  return bot;
}

function showStatus(msg, error=false) {
  const el = document.getElementById('status-bar');
  el.textContent = msg;
  el.style.color = error ? 'var(--red)' : 'var(--green)';
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 3000);
}

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _data = await r.json();
    renderAll(_data);
    document.getElementById('last-update').textContent =
      'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    showStatus('Fetch error: ' + e.message, true);
  }
}

function renderAll(d) {
  // Connection badges
  const db = document.getElementById('db-badge');
  db.textContent = 'DB ' + (d.db_connected ? 'OK' : 'DOWN');
  db.className = 'badge ' + (d.db_connected ? 'badge-green' : 'badge-red');
  const rd = document.getElementById('redis-badge');
  rd.textContent = 'REDIS ' + (d.redis_connected ? 'OK' : 'DOWN');
  rd.className = 'badge ' + (d.redis_connected ? 'badge-green' : 'badge-yellow');

  renderMemeCard(d.meme);
  renderCopyCard(d.copy);
  renderSniperCard(d.sniper);
  renderMemePositions(d.meme.open_positions);
  renderOtherPositions(d.copy.open_positions, d.sniper.open_positions);
  renderRecentTrades(d.recent_trades);
  renderConfigs(d.meme.config, d.copy.config, d.sniper.config);
}

function renderMemeCard(m) {
  const paused = m.paused;
  document.getElementById('meme-body').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Status</span>
      <span class="stat-value">${paused
        ? '<span class="badge badge-red">PAUSED</span>'
        : '<span class="badge badge-green">ACTIVE</span>'}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Wallet SOL</span>
      <span class="stat-value ${m.wallet_sol < 0.5 ? 'yellow' : 'green'}">${fmtSol(m.wallet_sol)}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Total Trades</span>
      <span class="stat-value">${m.total_trades}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win / Loss</span>
      <span class="stat-value"><span class="green">${m.winners}W</span> / <span class="red">${m.losers}L</span></span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win Rate</span>
      <span class="stat-value ${m.win_rate_pct >= 55 ? 'green' : m.win_rate_pct >= 45 ? 'yellow' : 'red'}">${fmt(m.win_rate_pct, 1)}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Total PnL</span>
      <span class="stat-value ${m.total_pnl_usd >= 0 ? 'green' : 'red'}">${m.total_pnl_usd >= 0 ? '+' : ''}${fmtUsd(m.total_pnl_usd)}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Daily Loss (SOL)</span>
      <span class="stat-value ${m.daily_loss_sol > 1.5 ? 'red' : m.daily_loss_sol > 0.5 ? 'yellow' : 'muted'}">${fmtSol(m.daily_loss_sol)} / ${fmtSol(m.config.daily_loss_limit_sol)}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Open Positions</span>
      <span class="stat-value">${m.open_positions.length} / ${m.config.max_open_positions || 8}</span>
    </div>
    ${m.wallet_pubkey ? `<div class="wallet-addr">${m.wallet_pubkey.slice(0,12)}...${m.wallet_pubkey.slice(-8)}</div>` : ''}
  `;
}

function renderCopyCard(c) {
  document.getElementById('copy-body').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Mode</span>
      <span class="stat-value">${c.mode === 'paper'
        ? '<span class="badge badge-yellow">PAPER</span>'
        : '<span class="badge badge-red">LIVE</span>'}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Wallet SOL</span>
      <span class="stat-value">${fmtSol(c.wallet_sol)}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Tracked Wallets</span>
      <span class="stat-value">${c.tracked_wallets}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Total Trades</span>
      <span class="stat-value">${c.total_trades}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Open Positions</span>
      <span class="stat-value">${c.open_positions.length} / ${c.config.max_open_positions || 5}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">SL / TP</span>
      <span class="stat-value">${c.config.stop_loss_pct || 25}% / ${c.config.take_profit_pct || 50}%</span>
    </div>
    ${c.wallet_pubkey ? `<div class="wallet-addr">${c.wallet_pubkey.slice(0,12)}...${c.wallet_pubkey.slice(-8)}</div>` : '<div class="wallet-addr muted">No wallet configured</div>'}
  `;
}

function renderSniperCard(s) {
  document.getElementById('sniper-body').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Mode</span>
      <span class="stat-value">${s.mode === 'paper'
        ? '<span class="badge badge-yellow">PAPER</span>'
        : '<span class="badge badge-red">LIVE</span>'}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Wallet SOL</span>
      <span class="stat-value">${fmtSol(s.wallet_sol)}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Paper Trades</span>
      <span class="stat-value">${s.total_trades}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Open Positions</span>
      <span class="stat-value">${s.open_positions.length} / ${s.config.max_open_positions || 5}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Curve Target</span>
      <span class="stat-value">${s.config.min_curve_pct || 25}% – ${s.config.max_curve_pct || 50}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">SL / TP1 / TP2</span>
      <span class="stat-value">${s.config.stop_loss_pct || 30}% / ${s.config.tp1_pct || 50}% / ${s.config.tp2_pct || 100}%</span>
    </div>
    ${s.wallet_pubkey ? `<div class="wallet-addr">${s.wallet_pubkey.slice(0,12)}...${s.wallet_pubkey.slice(-8)}</div>` : '<div class="wallet-addr muted">No wallet configured</div>'}
  `;
}

function renderMemePositions(positions) {
  if (!positions || positions.length === 0) {
    document.getElementById('meme-positions').innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const rows = positions.map(p => {
    const pnl = Number(p.live_pnl_pct || p.pnl_pct || 0);
    const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
    const sym = p.token_symbol || '???';
    const addr = p.token_address || '';
    const age = timeSince(p.opened_at);
    const stops = [];
    if (p.trailing_active) stops.push('<span class="yellow" title="Trailing stop active">&#9655;</span>');
    if (p.tier_1_done) stops.push('<span class="green" title="TP1 done">&#10003;1</span>');
    if (p.tier_2_done) stops.push('<span class="green" title="TP2 done">&#10003;2</span>');
    const stopIcons = stops.join(' ') || '';
    return `<tr class="${rowCls}">
      <td><a class="link" href="https://dexscreener.com/solana/${addr}" target="_blank">$${sym}</a></td>
      <td>${fmtMc(p.live_mc || p.current_mc || p.entry_mc)}</td>
      <td>${fmtPnl(pnl)}</td>
      <td>${fmtUsd(p.position_size_usd)}</td>
      <td>${fmt(p.remaining_pct || 100, 0)}%</td>
      <td>${stopIcons}</td>
      <td class="muted">${age}</td>
    </tr>`;
  }).join('');
  document.getElementById('meme-positions').innerHTML = `
    <table>
      <thead><tr>
        <th>Token</th><th>MC</th><th>PnL%</th><th>Size</th><th>Rem%</th><th>Flags</th><th>Age</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderOtherPositions(copyPos, snipePos) {
  const all = [
    ...(copyPos || []).map(p => ({...p, _bot: 'COPY', _sym: p.token_symbol || '???', _addr: p.token_address || ''})),
    ...(snipePos || []).map(p => ({...p, _bot: 'SNIPER', _sym: p.symbol || '???', _addr: p.mint || ''})),
  ];
  if (all.length === 0) {
    document.getElementById('other-positions').innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const rows = all.map(p => {
    const pnl = Number(p.pnl_pct || 0);
    const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
    const age = timeSince(p.opened_at);
    return `<tr class="${rowCls}">
      <td>${badgeBot(p._bot)}</td>
      <td><a class="link" href="https://dexscreener.com/solana/${p._addr}" target="_blank">$${p._sym}</a></td>
      <td>${fmtPnl(pnl)}</td>
      <td>${p.position_size_sol ? fmtSol(p.position_size_sol) : p.position_size_usd ? fmtUsd(p.position_size_usd) : '—'}</td>
      <td class="muted">${age}</td>
    </tr>`;
  }).join('');
  document.getElementById('other-positions').innerHTML = `
    <table>
      <thead><tr>
        <th>Bot</th><th>Token</th><th>PnL%</th><th>Size</th><th>Age</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderRecentTrades(trades) {
  if (!trades || trades.length === 0) {
    document.getElementById('recent-trades').innerHTML = '<div class="empty">No recent trades</div>';
    return;
  }
  const rows = trades.map(t => {
    const pnl = t.pnl_pct !== null && t.pnl_pct !== undefined ? Number(t.pnl_pct) : null;
    const isSell = (t.action || '').includes('SELL');
    const actionCls = isSell ? (pnl !== null && pnl > 0 ? 'green' : pnl !== null && pnl < 0 ? 'red' : 'muted') : 'blue';
    const addr = t.token_address || '';
    const sym = t.token || '???';
    return `<tr>
      <td class="muted">${timeSince(t.ts)}</td>
      <td>${badgeBot(t.bot)}</td>
      <td><span class="${actionCls}">${t.action || '—'}</span></td>
      <td><a class="link" href="https://dexscreener.com/solana/${addr}" target="_blank">$${sym}</a></td>
      <td>${isSell && pnl !== null ? fmtPnl(pnl) : '<span class="muted">—</span>'}</td>
      <td class="muted" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.reason || '—'}</td>
    </tr>`;
  }).join('');
  document.getElementById('recent-trades').innerHTML = `
    <table>
      <thead><tr>
        <th>When</th><th>Bot</th><th>Action</th><th>Token</th><th>PnL%</th><th>Reason</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function cfgItem(k, v) {
  let display = v;
  if (typeof v === 'boolean') display = v ? '<span class="green">YES</span>' : '<span class="red">NO</span>';
  else if (typeof v === 'object') display = '<span class="muted">[complex]</span>';
  return `<div class="cfg-item"><div class="cfg-key">${k}</div><div class="cfg-val">${display}</div></div>`;
}

function editItem(bot, k, v) {
  const type = typeof v === 'boolean' ? 'checkbox' : 'number';
  const step = typeof v === 'number' && v < 1 ? '0.001' : typeof v === 'number' && v < 100 ? '0.1' : '1';
  if (type === 'checkbox') {
    return `<div class="cfg-item"><label class="cfg-key">${k}</label><input type="checkbox" data-bot="${bot}" data-key="${k}" ${v ? 'checked' : ''} onchange="saveField(this)"></div>`;
  }
  return `<div class="cfg-item"><label class="cfg-key">${k}</label><input type="number" step="${step}" value="${v ?? ''}" data-bot="${bot}" data-key="${k}" onchange="saveField(this)" style="background:#1a1d2e;color:#e0e6ef;border:1px solid #2a2d3e;border-radius:4px;padding:2px 6px;width:90px;font-size:12px;"></div>`;
}

async function saveField(el) {
  const bot = el.dataset.bot;
  const key = el.dataset.key;
  const val = el.type === 'checkbox' ? el.checked : parseFloat(el.value);
  const endpoint = bot === 'meme' ? '/config' : bot === 'copy' ? '/config/copy' : '/config/sniper';
  try {
    const r = await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({[key]: val})});
    const j = await r.json();
    if (j.status === 'ok') {
      el.style.borderColor = '#00d9a3';
      setTimeout(() => el.style.borderColor = '#2a2d3e', 1500);
      showStatus(`Updated ${bot} ${key} = ${val}`);
    } else {
      el.style.borderColor = '#ff4444';
      showStatus('Error: ' + (j.detail || 'unknown'));
    }
  } catch(e) { el.style.borderColor = '#ff4444'; showStatus('Save failed: ' + e); }
}

function renderConfigs(mCfg, cCfg, sCfg) {
  const mKeys = ['base_position_sol','max_open_positions','daily_loss_limit_sol','stop_loss_pct',
                  'trailing_stop_activation_pct','trailing_stop_trail_pct','max_holding_hours',
                  'min_mc','max_mc','min_agiotage_score','min_sources','min_volume_h1',
                  'buy_slippage_bps','sell_slippage_bps','ratchet_stop_enabled','ratchet_stop_pct_of_tp'];
  document.getElementById('meme-config').innerHTML =
    `<div class="config-grid">${mKeys.map(k => editItem('meme', k, mCfg[k])).join('')}</div>`;

  const cKeys = ['position_size_sol','max_open_positions','daily_loss_limit_sol','stop_loss_pct',
                  'take_profit_pct','max_hold_hours','min_wallet_winrate','min_mc','min_volume_h1',
                  'min_buy_usd','paper_mode','enabled'];
  const sKeys = ['position_size_sol','max_open_positions','daily_loss_limit_sol','stop_loss_pct',
                  'tp1_pct','tp1_sell_pct','tp2_pct','tp2_sell_pct','min_curve_pct','max_curve_pct',
                  'min_holders','min_volume_sol','max_hold_minutes','paper_mode','enabled'];
  document.getElementById('other-config').innerHTML = `
    <div class="section-title">Copy Bot</div>
    <div class="config-grid">${cKeys.map(k => editItem('copy', k, cCfg[k])).join('')}</div>
    <div class="section-title" style="margin-top:12px">Sniper Bot</div>
    <div class="config-grid">${sKeys.map(k => editItem('sniper', k, sCfg[k])).join('')}</div>
  `;
}

async function pauseBot() {
  if (!confirm('Pause meme bot? (sets daily_loss_limit to 0.001 SOL)')) return;
  try {
    const r = await fetch('/control/pause', {method: 'POST'});
    const j = await r.json();
    showStatus('Meme bot PAUSED — ' + j.status);
    await fetchStatus();
  } catch(e) { showStatus('Error: ' + e.message, true); }
}

async function unpauseBot() {
  if (!confirm('Unpause meme bot? (restores daily_loss_limit to 2.0 SOL)')) return;
  try {
    const r = await fetch('/control/unpause', {method: 'POST'});
    const j = await r.json();
    showStatus('Meme bot UNPAUSED — daily limit: ' + j.daily_loss_limit_sol + ' SOL');
    await fetchStatus();
  } catch(e) { showStatus('Error: ' + e.message, true); }
}

async function sellStuck(bot) {
  if (!confirm(`Sell ALL stuck ${bot} tokens on-chain via Jupiter?\\nThis executes real sells.`)) return;
  showStatus(`Selling ${bot} tokens... please wait`);
  try {
    const r = await fetch(`/control/sell-stuck/${bot}`, {method: 'POST'});
    const j = await r.json();
    showStatus(`${bot}: ${j.tokens_sold} sold, ${j.tokens_failed} failed, ${j.db_positions_closed} DB closed`);
    await fetchStatus();
  } catch(e) { showStatus('Error: ' + e.message, true); }
}

function refreshNow() { fetchStatus(); resetCountdown(); }

function resetCountdown() {
  _countdown = 10;
  if (_timer) clearInterval(_timer);
  _timer = setInterval(() => {
    _countdown--;
    document.getElementById('refresh-timer').textContent = 'Refreshing in ' + _countdown + 's';
    if (_countdown <= 0) {
      fetchStatus();
      _countdown = 10;
    }
  }, 1000);
}

// Boot
fetchStatus();
resetCountdown();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=_HTML)


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  Agiotage Bot Dashboard")
    print("  http://localhost:8080")
    print("=" * 60)
    uvicorn.run(
        "local_dashboard:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )

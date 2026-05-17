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
    "min_mc": 550000,
    "max_mc": 5000000,
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
    "max_holding_hours": 4,
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
    "emergency_exit_pct": 15,
    "emergency_exit_seconds": 10,
    "stop_loss_pct": 20,
    "tp1_pct": 25,
    "tp1_sell_pct": 50,
    "tp2_pct": 50,
    "tp2_sell_pct": 50,
    "trailing_activate_pct": 15,
    "trailing_distance_pct": 15,
    "max_hold_hours": 2,
}

SNIPER_DEFAULTS = {
    "enabled": True,
    "paper_mode": True,
    "position_size_sol": 0.05,
    "max_open_positions": 5,
    "daily_loss_limit_sol": 0.50,
    "min_curve_pct": 25,
    "max_curve_pct": 40,
    "min_holders": 10,
    "max_holders": 20,
    "min_buy_sell_ratio": 2.0,
    "emergency_exit_pct": 12,
    "emergency_exit_seconds": 5,
    "stop_loss_pct": 20,
    "tp1_pct": 30,
    "tp1_sell_pct": 50,
    "tp2_pct": 60,
    "tp2_sell_pct": 50,
    "trailing_activate_pct": 15,
    "trailing_distance_pct": 10,
    "stagnation_seconds": 20,
    "max_hold_minutes": 15,
}

MIGRATION_DEFAULTS = {
    "enabled": True,
    "paper_mode": True,
    "position_size_sol": 0.10,
    "max_open_positions": 3,
    "daily_loss_limit_sol": 0.50,
    "graduation_real_sol": 85,
    "min_real_sol": 72,
    "max_real_sol": 84,
    "min_holders": 5,
    "min_volume_sol": 10.0,
    "emergency_exit_pct": 15,
    "emergency_exit_seconds": 10,
    "migration_sell_pct": 80,
    "migration_wait_seconds": 8,
    "stop_loss_pct": 20,
    "trailing_distance_pct": 10,
    "stagnation_seconds": 30,
    "no_migration_timeout_minutes": 5,
    "moonbag_max_hold_seconds": 120,
    "max_hold_minutes": 5,
}

MOMENTUM_DEFAULTS = {
    "enabled": True,
    "paper_mode": True,
    "position_size_sol": 0.10,
    "max_open_positions": 3,
    "daily_loss_limit_sol": 0.30,
    "min_m5_pct": 5.0,
    "min_buy_sell_ratio_m5": 1.5,
    "min_mc_usd": 100000,
    "max_mc_usd": 10000000,
    "min_liquidity_usd": 10000,
    "tp1_pct": 15,
    "tp1_sell_pct": 50,
    "tp2_pct": 30,
    "stop_loss_pct": 8,
    "trailing_distance_pct": 8,
    "stagnation_seconds": 30,
    "max_hold_seconds": 300,
}

WHALE_DEFAULTS = {
    "enabled": True,
    "paper_mode": True,
    "min_buy_usd": 900,
    "size_900": 0.15,
    "size_2000": 0.25,
    "size_5000": 0.40,
    "max_open_positions": 8,
    "daily_loss_limit_sol": 0.50,
    "trailing_stop_pct": 25,
    "hard_stop_pct": 25,
    "min_mc_usd": 10000,
    "max_mc_usd": 50000000,
    "cooldown_hours": 4,
}

DIP_BUYER_DEFAULTS = {
    "enabled": True,
    "paper_mode": True,
    "position_size_sol": 0.15,
    "max_open_positions": 5,
    "daily_loss_limit_sol": 0.50,
    "reversal_pct": 5,
    "min_dump_pct": 5,
    "max_dump_pct": 60,
    "max_wait_seconds": 120,
    "tp1_pct": 100,
    "tp1_sell_pct": 50,
    "tp2_pct": 200,
    "tp2_sell_pct": 50,
    "trailing_stop_pct": 25,
    "hard_stop_pct": 25,
}

CONVERGENCE_DEFAULTS = {
    "enabled": True,
    "paper_mode": True,
    "position_size_sol": 0.15,
    "max_open_positions": 3,
    "daily_loss_limit_sol": 0.50,
    "min_convergence_sources": 3,
    "convergence_window_seconds": 60,
    "min_mc_usd": 100000,
    "max_mc_usd": 5000000,
    "tp1_pct": 20,
    "tp1_sell_pct": 34,
    "tp2_pct": 50,
    "tp2_sell_pct": 33,
    "trailing_distance_pct": 15,
    "hard_stop_pct": 15,
    "cooldown_hours": 4,
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
    momentum_pubkey = _resolve_wallet_pubkey("MIGRATION_WALLET_PRIVATE_KEY")
    whale_pubkey = _resolve_wallet_pubkey("WHALE_FOLLOW_PRIVATE_KEY")

    meme_bal, copy_bal, sniper_bal, momentum_bal, whale_bal = await asyncio.gather(
        _fetch_sol_balance(meme_pubkey or ""),
        _fetch_sol_balance(copy_pubkey or ""),
        _fetch_sol_balance(sniper_pubkey or ""),
        _fetch_sol_balance(momentum_pubkey or ""),
        _fetch_sol_balance(whale_pubkey or ""),
        return_exceptions=True,
    )
    meme_bal = meme_bal if isinstance(meme_bal, float) else 0.0
    copy_bal = copy_bal if isinstance(copy_bal, float) else 0.0
    sniper_bal = sniper_bal if isinstance(sniper_bal, float) else 0.0
    momentum_bal = momentum_bal if isinstance(momentum_bal, float) else 0.0
    whale_bal = whale_bal if isinstance(whale_bal, float) else 0.0

    # ── Wallet-wide P&L ─────────────────────────────────────────────────────────
    wallet_pnl = await _query_one("""
        SELECT
            COALESCE(SUM(CASE WHEN action LIKE 'SELL%' THEN usd_value ELSE 0 END), 0) AS meme_realized_usd,
            COALESCE(SUM(CASE WHEN action = 'BUY' THEN usd_value ELSE 0 END), 0) AS meme_invested_usd
        FROM paper_trades
    """)
    copy_pnl = await _query_one("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct > 0) AS wins,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct <= 0) AS losses,
            COALESCE(AVG(pnl_pct) FILTER (WHERE status = 'CLOSED'), 0) AS avg_pnl,
            COALESCE(SUM(position_size_sol), 0) AS total_invested_sol
        FROM copy_positions
    """)
    sniper_pnl = await _query_one("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct > 0) AS wins,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct <= 0) AS losses,
            COALESCE(AVG(pnl_pct) FILTER (WHERE status = 'CLOSED'), 0) AS avg_pnl,
            COALESCE(SUM(position_size_sol), 0) AS total_invested_sol
        FROM snipe_positions
    """)

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

    # ── Migration sniper stats ────────────────────────────────────────────────
    migration_config = await _get_redis_config("migration_sniper_config", MIGRATION_DEFAULTS)
    migration_stats = await _query_one("""
        SELECT COUNT(*) AS total_trades FROM migration_positions
    """)
    migration_open = await _query("""
        SELECT
            mp.id, mp.symbol, mp.mint, mp.entry_price_sol,
            mp.current_price_sol, mp.position_size_sol,
            mp.pnl_pct, mp.entry_curve_pct, mp.migrated, mp.remaining_pct,
            mp.entry_holders, mp.highest_price_sol,
            mp.opened_at, mp.status, mp.close_reason
        FROM migration_positions mp
        WHERE mp.status = 'OPEN'
        ORDER BY mp.opened_at DESC
    """)
    migration_pnl = await _query_one("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct > 0) AS wins,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct <= 0) AS losses,
            COALESCE(AVG(pnl_pct) FILTER (WHERE status = 'CLOSED'), 0) AS avg_pnl
        FROM migration_positions
    """)

    # ── Momentum bot stats ────────────────────────────────────────────────────
    momentum_config = await _get_redis_config("momentum_bot_config", MOMENTUM_DEFAULTS)
    momentum_stats = await _query_one("""
        SELECT COUNT(*) AS total_trades FROM momentum_positions
    """)
    momentum_open = await _query("""
        SELECT
            mp.id, mp.token_symbol, mp.token_address, mp.entry_price,
            mp.current_price, mp.position_size_sol,
            mp.pnl_pct, mp.entry_m5_pct, mp.entry_buy_ratio,
            mp.remaining_pct, mp.highest_price,
            mp.opened_at, mp.status, mp.close_reason
        FROM momentum_positions mp
        WHERE mp.status = 'OPEN'
        ORDER BY mp.opened_at DESC
    """)
    momentum_pnl = await _query_one("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct > 0) AS wins,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct <= 0) AS losses,
            COALESCE(AVG(pnl_pct) FILTER (WHERE status = 'CLOSED'), 0) AS avg_pnl
        FROM momentum_positions
    """)

    # ── Whale Follow bot stats ────────────────────────────────────────────────
    whale_config = await _get_redis_config("whale_follow_config", WHALE_DEFAULTS)
    whale_stats = await _query_one("""
        SELECT COUNT(*) AS total_trades FROM whale_positions
    """)
    whale_open = await _query("""
        SELECT
            wp.id, wp.token_symbol, wp.token_address,
            wp.entry_price, wp.current_price, wp.highest_price,
            wp.position_size_sol, wp.pnl_pct, wp.whale_wallet,
            wp.whale_buy_usd, wp.entry_mc, wp.remaining_pct, wp.tp1_done,
            wp.opened_at, wp.status, wp.close_reason
        FROM whale_positions wp
        WHERE wp.status = 'OPEN'
        ORDER BY wp.opened_at DESC
    """)
    whale_pnl = await _query_one("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct > 0) AS wins,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct <= 0) AS losses,
            COALESCE(AVG(pnl_pct) FILTER (WHERE status = 'CLOSED'), 0) AS avg_pnl
        FROM whale_positions
    """)
    whale_wallets_count = await _query_one(
        "SELECT COUNT(*) AS cnt FROM whale_tracked_wallets WHERE active = TRUE"
    )

    # ── Whale: enrich open positions with live current_mc ─────────────────
    whale_price_tasks = {}
    for wp in whale_open:
        addr = wp.get("token_address", "")
        if addr and addr not in whale_price_tasks:
            whale_price_tasks[addr] = _fetch_token_price(addr)
    if whale_price_tasks:
        whale_price_results = dict(zip(
            whale_price_tasks.keys(),
            await asyncio.gather(*whale_price_tasks.values(), return_exceptions=True),
        ))
    else:
        whale_price_results = {}
    for wp in whale_open:
        addr = wp.get("token_address", "")
        result = whale_price_results.get(addr)
        if isinstance(result, tuple):
            live_price, live_mc = result
            if live_price > 0:
                entry = float(wp.get("entry_price") or 0)
                wp["current_mc"] = live_mc
                wp["current_price"] = live_price
                if entry > 0:
                    wp["pnl_pct"] = round((live_price - entry) / entry * 100, 2)
            else:
                wp["current_mc"] = 0
        else:
            wp["current_mc"] = 0

    # ── Whale: recent closed trades ───────────────────────────────────────
    whale_closed_recent = await _query("""
        SELECT
            wp.id, wp.token_symbol, wp.token_address,
            wp.entry_price, wp.current_price, wp.highest_price,
            wp.position_size_sol, wp.pnl_pct, wp.whale_wallet,
            wp.whale_buy_usd, wp.entry_mc, wp.close_reason,
            wp.opened_at, wp.closed_at, wp.status
        FROM whale_positions wp
        WHERE wp.status = 'CLOSED'
        ORDER BY wp.closed_at DESC NULLS LAST
        LIMIT 10
    """)

    # ── Dip Buyer bot stats ────────────────────────────────────────────────────
    dip_config = await _get_redis_config("dip_buyer_config", DIP_BUYER_DEFAULTS)
    dip_stats = await _query_one("""
        SELECT COUNT(*) AS total_trades FROM dip_positions
    """)
    dip_open = await _query("""
        SELECT
            dp.id, dp.token_symbol, dp.token_address,
            dp.graduation_price, dp.dump_low_price, dp.dump_pct,
            dp.entry_price, dp.current_price, dp.highest_price,
            dp.position_size_sol, dp.pnl_pct, dp.remaining_pct,
            dp.opened_at, dp.status, dp.close_reason
        FROM dip_positions dp
        WHERE dp.status = 'OPEN'
        ORDER BY dp.opened_at DESC
    """)
    dip_pnl = await _query_one("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct > 0) AS wins,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct <= 0) AS losses,
            COALESCE(AVG(pnl_pct) FILTER (WHERE status = 'CLOSED'), 0) AS avg_pnl
        FROM dip_positions
    """)

    # ── Convergence bot stats ────────────────────────────────────────────────
    convergence_config = await _get_redis_config("convergence_bot_config", CONVERGENCE_DEFAULTS)
    convergence_stats = await _query_one("SELECT COUNT(*) AS total_trades FROM convergence_positions")
    convergence_open = await _query("""
        SELECT cp.id, cp.token_symbol, cp.token_address, cp.entry_price, cp.current_price,
            cp.highest_price, cp.position_size_sol, cp.pnl_pct, cp.remaining_pct,
            cp.signal_sources, cp.signal_count, cp.opened_at, cp.status, cp.close_reason
        FROM convergence_positions cp WHERE cp.status = 'OPEN' ORDER BY cp.opened_at DESC
    """)
    convergence_pnl = await _query_one("""
        SELECT COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct > 0) AS wins,
            COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_pct <= 0) AS losses,
            COALESCE(AVG(pnl_pct) FILTER (WHERE status = 'CLOSED'), 0) AS avg_pnl
        FROM convergence_positions
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

    recent_migration = await _query("""
        SELECT
            mt.created_at AS ts,
            mt.action,
            mp.symbol AS token,
            mp.mint AS token_address,
            mt.pnl_pct,
            mt.reason,
            'MIGRATION' AS bot
        FROM migration_trades mt
        JOIN migration_positions mp ON mt.position_id = mp.id
        ORDER BY mt.created_at DESC
        LIMIT 10
    """)

    recent_momentum = await _query("""
        SELECT
            mt.created_at AS ts,
            mt.action,
            mp.symbol AS token,
            mp.mint AS token_address,
            mt.pnl_pct,
            mt.reason,
            'MOMENTUM' AS bot
        FROM momentum_trades mt
        JOIN momentum_positions mp ON mt.position_id = mp.id
        ORDER BY mt.created_at DESC
        LIMIT 10
    """)

    recent_whale = await _query("""
        SELECT
            wt.created_at AS ts,
            wt.action,
            wp.token_symbol AS token,
            wp.token_address,
            wt.pnl_pct,
            wt.reason,
            'WHALE' AS bot
        FROM whale_trades wt
        JOIN whale_positions wp ON wt.position_id = wp.id
        ORDER BY wt.created_at DESC
        LIMIT 10
    """)

    recent_dip = await _query("""
        SELECT
            dt.created_at AS ts,
            dt.action,
            dp.token_symbol AS token,
            dp.token_address,
            dt.pnl_pct,
            dt.reason,
            'DIP' AS bot
        FROM dip_trades dt
        JOIN dip_positions dp ON dt.position_id = dp.id
        ORDER BY dt.created_at DESC
        LIMIT 10
    """)

    # Merge + sort
    all_trades = recent_meme + recent_copy + recent_snipe + recent_migration + recent_momentum + recent_whale + recent_dip
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

    def _mask_key(env_name: str) -> str:
        k = os.environ.get(env_name, "")
        if not k or len(k) < 8:
            return ""
        return k[:4] + "..." + k[-4:]

    return {
        "ts": now.isoformat(),
        "db_connected": _pg_pool is not None,
        "redis_connected": _redis is not None,
        "wallets": {
            "meme": {"pubkey": meme_pubkey, "key_set": bool(os.environ.get("TRADING_WALLET_PRIVATE_KEY")), "key_masked": _mask_key("TRADING_WALLET_PRIVATE_KEY"), "paper_mode": meme_config.get("paper_mode", True)},
            "copy": {"pubkey": copy_pubkey, "key_set": bool(os.environ.get("COPY_TRADER_PRIVATE_KEY")), "key_masked": _mask_key("COPY_TRADER_PRIVATE_KEY"), "paper_mode": copy_config.get("paper_mode", True)},
            "sniper": {"pubkey": sniper_pubkey, "key_set": bool(os.environ.get("SNIPER_WALLET_PRIVATE_KEY")), "key_masked": _mask_key("SNIPER_WALLET_PRIVATE_KEY"), "paper_mode": sniper_config.get("paper_mode", True)},
            "migration": {"pubkey": _resolve_wallet_pubkey("MIGRATION_WALLET_PRIVATE_KEY"), "key_set": bool(os.environ.get("MIGRATION_WALLET_PRIVATE_KEY")), "key_masked": _mask_key("MIGRATION_WALLET_PRIVATE_KEY"), "paper_mode": migration_config.get("paper_mode", True)},
            "momentum": {"pubkey": momentum_pubkey, "key_set": bool(os.environ.get("MIGRATION_WALLET_PRIVATE_KEY")), "key_masked": _mask_key("MIGRATION_WALLET_PRIVATE_KEY"), "paper_mode": momentum_config.get("paper_mode", True)},
            "whale": {"pubkey": whale_pubkey, "key_set": bool(os.environ.get("WHALE_FOLLOW_PRIVATE_KEY")), "key_masked": _mask_key("WHALE_FOLLOW_PRIVATE_KEY"), "paper_mode": whale_config.get("paper_mode", True)},
            "convergence": {"pubkey": whale_pubkey, "key_set": bool(os.environ.get("WHALE_FOLLOW_PRIVATE_KEY")), "key_masked": _mask_key("WHALE_FOLLOW_PRIVATE_KEY"), "paper_mode": convergence_config.get("paper_mode", True)},
        },
        "wallet_pnl": {
            "total_sol_balance": round(meme_bal + copy_bal + sniper_bal, 4),
            "meme_wallet_sol": round(meme_bal, 4),
            "copy_wallet_sol": round(copy_bal, 4),
            "sniper_wallet_sol": round(sniper_bal, 4),
            "meme_realized_usd": round(float((wallet_pnl or {}).get("meme_realized_usd") or 0), 2),
            "meme_invested_usd": round(float((wallet_pnl or {}).get("meme_invested_usd") or 0), 2),
            "copy_closed": int((copy_pnl or {}).get("closed") or 0),
            "copy_wins": int((copy_pnl or {}).get("wins") or 0),
            "copy_losses": int((copy_pnl or {}).get("losses") or 0),
            "copy_avg_pnl": round(float((copy_pnl or {}).get("avg_pnl") or 0), 2),
            "copy_invested_sol": round(float((copy_pnl or {}).get("total_invested_sol") or 0), 4),
            "sniper_closed": int((sniper_pnl or {}).get("closed") or 0),
            "sniper_wins": int((sniper_pnl or {}).get("wins") or 0),
            "sniper_losses": int((sniper_pnl or {}).get("losses") or 0),
            "sniper_avg_pnl": round(float((sniper_pnl or {}).get("avg_pnl") or 0), 2),
            "sniper_invested_sol": round(float((sniper_pnl or {}).get("total_invested_sol") or 0), 4),
        },
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
        "migration": {
            "mode": "paper" if migration_config.get("paper_mode", True) else "live",
            "total_trades": int((migration_stats or {}).get("total_trades") or 0),
            "closed": int((migration_pnl or {}).get("closed") or 0),
            "wins": int((migration_pnl or {}).get("wins") or 0),
            "losses": int((migration_pnl or {}).get("losses") or 0),
            "avg_pnl": round(float((migration_pnl or {}).get("avg_pnl") or 0), 2),
            "open_positions": _clean(migration_open),
            "config": migration_config,
        },
        "momentum": {
            "mode": "paper" if momentum_config.get("paper_mode", True) else "live",
            "total_trades": int((momentum_stats or {}).get("total_trades") or 0),
            "closed": int((momentum_pnl or {}).get("closed") or 0),
            "wins": int((momentum_pnl or {}).get("wins") or 0),
            "losses": int((momentum_pnl or {}).get("losses") or 0),
            "avg_pnl": round(float((momentum_pnl or {}).get("avg_pnl") or 0), 2),
            "wallet_pubkey": momentum_pubkey,
            "wallet_sol": momentum_bal,
            "open_positions": _clean(momentum_open),
            "config": momentum_config,
        },
        "whale": {
            "mode": "paper" if whale_config.get("paper_mode", True) else "live",
            "total_trades": int((whale_stats or {}).get("total_trades") or 0),
            "closed": int((whale_pnl or {}).get("closed") or 0),
            "wins": int((whale_pnl or {}).get("wins") or 0),
            "losses": int((whale_pnl or {}).get("losses") or 0),
            "avg_pnl": round(float((whale_pnl or {}).get("avg_pnl") or 0), 2),
            "tracked_wallets": int((whale_wallets_count or {}).get("cnt") or 0),
            "wallet_pubkey": whale_pubkey,
            "wallet_sol": whale_bal,
            "open_positions": _clean(whale_open),
            "closed_recent": _clean(whale_closed_recent),
            "config": whale_config,
        },
        "dip_buyer": {
            "mode": "paper" if dip_config.get("paper_mode", True) else "live",
            "total_trades": int((dip_stats or {}).get("total_trades") or 0),
            "closed": int((dip_pnl or {}).get("closed") or 0),
            "wins": int((dip_pnl or {}).get("wins") or 0),
            "losses": int((dip_pnl or {}).get("losses") or 0),
            "avg_pnl": round(float((dip_pnl or {}).get("avg_pnl") or 0), 2),
            "open_positions": _clean(dip_open),
            "config": dip_config,
        },
        "convergence": {
            "mode": "paper" if convergence_config.get("paper_mode", True) else "live",
            "total_trades": int((convergence_stats or {}).get("total_trades") or 0),
            "closed": int((convergence_pnl or {}).get("closed") or 0),
            "wins": int((convergence_pnl or {}).get("wins") or 0),
            "losses": int((convergence_pnl or {}).get("losses") or 0),
            "avg_pnl": round(float((convergence_pnl or {}).get("avg_pnl") or 0), 2),
            "open_positions": _clean(convergence_open),
            "config": convergence_config,
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
        "emergency_exit_pct", "emergency_exit_seconds",
        "stop_loss_pct", "tp1_pct", "tp1_sell_pct", "tp2_pct", "tp2_sell_pct",
        "trailing_activate_pct", "trailing_distance_pct",
        "max_hold_hours", "min_wallet_winrate",
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
        "emergency_exit_pct", "emergency_exit_seconds",
        "stop_loss_pct", "tp1_pct", "tp1_sell_pct", "tp2_pct", "tp2_sell_pct",
        "trailing_activate_pct", "trailing_distance_pct",
        "stagnation_seconds", "max_hold_minutes",
        "min_curve_pct", "max_curve_pct",
        "min_holders", "max_holders", "min_buy_sell_ratio",
        "min_volume_sol", "max_dev_pct", "paper_mode", "enabled",
        "slippage_pct", "max_token_age_minutes",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields")
    new_cfg = await _set_redis_config("pumpfun_sniper_config", filtered, SNIPER_DEFAULTS)
    return {"status": "ok", "updated": filtered, "config": new_cfg}


@app.post("/config/migration")
async def update_migration_config(request: Request):
    """Update migration sniper config."""
    try:
        updates = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    allowed = {
        "position_size_sol", "max_open_positions", "daily_loss_limit_sol",
        "graduation_real_sol", "min_real_sol", "max_real_sol",
        "emergency_exit_pct", "emergency_exit_seconds",
        "migration_sell_pct", "migration_wait_seconds",
        "stop_loss_pct", "trailing_distance_pct", "stagnation_seconds",
        "max_hold_minutes",
        "min_holders", "min_volume_sol", "max_dev_pct", "paper_mode", "enabled",
        "slippage_pct", "max_token_age_minutes",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields")
    new_cfg = await _set_redis_config("migration_sniper_config", filtered, MIGRATION_DEFAULTS)
    return {"status": "ok", "updated": filtered, "config": new_cfg}


@app.post("/config/momentum")
async def update_momentum_config(request: Request):
    """Update momentum breakout bot config."""
    try:
        updates = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    allowed = {
        "position_size_sol", "max_open_positions", "daily_loss_limit_sol",
        "min_m5_pct", "min_buy_sell_ratio_m5",
        "min_mc_usd", "max_mc_usd", "min_liquidity_usd",
        "tp1_pct", "tp1_sell_pct", "tp2_pct",
        "stop_loss_pct", "trailing_distance_pct",
        "stagnation_seconds", "max_hold_seconds",
        "paper_mode", "enabled",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields")
    new_cfg = await _set_redis_config("momentum_bot_config", filtered, MOMENTUM_DEFAULTS)
    return {"status": "ok", "updated": filtered, "config": new_cfg}


@app.post("/config/whale")
async def update_whale_config(request: Request):
    """Update whale follow bot config."""
    try:
        updates = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    allowed = {
        "min_buy_usd", "size_900", "size_2000", "size_5000",
        "max_open_positions", "daily_loss_limit_sol",
        "trailing_stop_pct", "hard_stop_pct",
        "min_mc_usd", "max_mc_usd", "cooldown_hours",
        "paper_mode", "enabled",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields")
    new_cfg = await _set_redis_config("whale_follow_config", filtered, WHALE_DEFAULTS)
    return {"status": "ok", "updated": filtered, "config": new_cfg}


@app.post("/config/dip")
async def update_dip_config(request: Request):
    """Update dip buyer bot config."""
    try:
        updates = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    allowed = {
        "enabled", "paper_mode",
        "position_size_sol", "max_open_positions", "daily_loss_limit_sol",
        "reversal_pct", "min_dump_pct", "max_dump_pct", "max_wait_seconds",
        "tp1_pct", "tp1_sell_pct", "tp2_pct", "tp2_sell_pct",
        "trailing_stop_pct", "hard_stop_pct",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields")
    new_cfg = await _set_redis_config("dip_buyer_config", filtered, DIP_BUYER_DEFAULTS)
    return {"status": "ok", "updated": filtered, "config": new_cfg}


@app.post("/config/convergence")
async def update_convergence_config(request: Request):
    """Update convergence bot config."""
    try:
        updates = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    allowed = {
        "position_size_sol", "max_open_positions", "daily_loss_limit_sol",
        "min_convergence_sources", "convergence_window_seconds",
        "min_mc_usd", "max_mc_usd", "min_liquidity_usd",
        "tp1_pct", "tp1_sell_pct", "tp2_pct", "tp2_sell_pct",
        "trailing_distance_pct", "hard_stop_pct", "cooldown_hours",
        "paper_mode", "enabled",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields")
    new_cfg = await _set_redis_config("convergence_bot_config", filtered, CONVERGENCE_DEFAULTS)
    return {"status": "ok", "updated": filtered, "config": new_cfg}


@app.post("/control/sell-stuck/convergence")
async def sell_stuck_convergence():
    """Sell all stuck convergence bot tokens on-chain via Jupiter."""
    return await _sell_stuck_tokens("convergence")


@app.post("/control/sell-whale-token/{position_id}")
async def sell_whale_token(position_id: int):
    """Sell a single whale position by ID on-chain via Jupiter."""
    if not _pg_pool:
        raise HTTPException(status_code=503, detail="Database not connected")

    # Look up the position
    pos = await _query_one(
        "SELECT id, token_address, token_symbol, status FROM whale_positions WHERE id = $1",
        position_id,
    )
    if not pos:
        raise HTTPException(status_code=404, detail=f"Whale position {position_id} not found")
    if pos["status"] != "OPEN":
        raise HTTPException(status_code=400, detail=f"Position {position_id} is already {pos['status']}")

    wallet_key = os.environ.get("WHALE_FOLLOW_PRIVATE_KEY", "") or _TRADING_WALLET_KEY
    if not wallet_key:
        raise HTTPException(status_code=503, detail="No wallet key for whale (WHALE_FOLLOW_PRIVATE_KEY)")

    try:
        from solders.keypair import Keypair
        import base58 as b58
        pk = wallet_key
        if pk.startswith("["):
            kp = Keypair.from_bytes(bytes(json.loads(pk)))
        else:
            kp = Keypair.from_bytes(b58.b58decode(pk))
        wallet_addr = str(kp.pubkey())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Wallet key error: {e}")

    mint = pos["token_address"]
    sold = False
    tx_hash = ""
    error_msg = ""

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
                if resp.status_code != 200:
                    continue
                accounts = resp.json().get("result", {}).get("value", [])
                for acc in accounts:
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    acc_mint = info.get("mint", "")
                    raw = int(info.get("tokenAmount", {}).get("amount", 0) or 0)
                    if acc_mint == mint and raw > 0:
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
                                error_msg = "quote failed"
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
                                error_msg = "swap failed"
                                continue
                            swap_tx = swap_resp.json().get("swapTransaction")
                            if not swap_tx:
                                error_msg = "no tx"
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
                                sold = True
                            else:
                                error_msg = "send failed"
                        except Exception as e:
                            error_msg = str(e)[:80]
            except Exception as e:
                error_msg = str(e)[:80]

    # Close the DB position regardless
    try:
        async with _pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE whale_positions SET status = 'CLOSED', close_reason = 'Dashboard: manual sell' WHERE id = $1",
                position_id,
            )
    except Exception:
        pass

    return {
        "status": "ok",
        "position_id": position_id,
        "token": pos.get("token_symbol", ""),
        "sold_onchain": sold,
        "tx_hash": tx_hash[:20] if tx_hash else "",
        "error": error_msg if not sold else "",
    }


@app.post("/wallet/save")
async def save_wallet_key(request: Request):
    """Save a wallet private key to .env.local."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    bot = data.get("bot", "")
    private_key = data.get("private_key", "").strip()
    if not private_key:
        raise HTTPException(status_code=400, detail="No private key provided")

    env_map = {
        "meme": "TRADING_WALLET_PRIVATE_KEY",
        "copy": "COPY_TRADER_PRIVATE_KEY",
        "sniper": "SNIPER_WALLET_PRIVATE_KEY",
        "migration": "MIGRATION_WALLET_PRIVATE_KEY",
        "momentum": "MIGRATION_WALLET_PRIVATE_KEY",
        "whale": "WHALE_FOLLOW_PRIVATE_KEY",
    }
    env_key = env_map.get(bot)
    if not env_key:
        raise HTTPException(status_code=400, detail=f"Unknown bot: {bot}")

    # Validate the key by trying to derive a pubkey
    try:
        from solders.keypair import Keypair
        if private_key.startswith("["):
            import json as _j
            kp = Keypair.from_bytes(bytes(_j.loads(private_key)))
        else:
            import base58 as b58
            kp = Keypair.from_bytes(b58.b58decode(private_key))
        pubkey = str(kp.pubkey())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid private key: {e}")

    # Update .env.local
    env_path = Path(__file__).parent / ".env.local"
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{env_key}="):
                lines[i] = f"{env_key}={private_key}"
                found = True
                break
        if not found:
            lines.append(f"{env_key}={private_key}")
        env_path.write_text("\n".join(lines) + "\n")
    else:
        env_path.write_text(f"{env_key}={private_key}\n")

    # Update runtime env
    os.environ[env_key] = private_key

    _log.info(f"Wallet key saved for {bot}: {pubkey[:12]}...")
    return {"status": "ok", "bot": bot, "pubkey": pubkey, "env_key": env_key}


@app.post("/wallet/toggle-live")
async def toggle_live_mode(request: Request):
    """Toggle paper/live mode for a bot."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    bot = data.get("bot", "")
    live = data.get("live", False)

    config_map = {
        "meme": ("paper_trader_config", MEME_DEFAULTS),
        "copy": ("copy_trader_config", COPY_DEFAULTS),
        "sniper": ("pumpfun_sniper_config", SNIPER_DEFAULTS),
        "migration": ("migration_sniper_config", MIGRATION_DEFAULTS),
        "momentum": ("momentum_bot_config", MOMENTUM_DEFAULTS),
        "whale": ("whale_follow_config", WHALE_DEFAULTS),
        "dip": ("dip_buyer_config", DIP_BUYER_DEFAULTS),
        "convergence": ("convergence_bot_config", CONVERGENCE_DEFAULTS),
    }
    config_info = config_map.get(bot)
    if not config_info:
        raise HTTPException(status_code=400, detail=f"Unknown bot: {bot}")

    redis_key, defaults = config_info

    # Check wallet key is set before allowing live mode
    if live:
        env_keys = {
            "meme": "TRADING_WALLET_PRIVATE_KEY",
            "copy": "COPY_TRADER_PRIVATE_KEY",
            "sniper": "SNIPER_WALLET_PRIVATE_KEY",
            "migration": "MIGRATION_WALLET_PRIVATE_KEY",
            "momentum": "MIGRATION_WALLET_PRIVATE_KEY",
            "whale": "WHALE_FOLLOW_PRIVATE_KEY",
            "dip": "MIGRATION_WALLET_PRIVATE_KEY",
            "convergence": "WHALE_FOLLOW_PRIVATE_KEY",
        }
        key_val = os.environ.get(env_keys.get(bot, ""), "")
        if not key_val:
            raise HTTPException(status_code=400, detail=f"Cannot go live: wallet key not set. Save a wallet key first.")

    new_cfg = await _set_redis_config(redis_key, {"paper_mode": not live}, defaults)
    mode = "LIVE" if live else "PAPER"
    _log.info(f"{bot} bot switched to {mode} mode")
    return {"status": "ok", "bot": bot, "mode": mode, "paper_mode": new_cfg.get("paper_mode")}


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


@app.post("/control/sell-stuck/migration")
async def sell_stuck_migration():
    """Sell all stuck migration sniper tokens on-chain via Jupiter."""
    return await _sell_stuck_tokens("migration")


@app.post("/control/sell-stuck/whale")
async def sell_stuck_whale():
    """Sell all stuck whale follow bot tokens on-chain via Jupiter."""
    return await _sell_stuck_tokens("whale")


@app.post("/control/sell-stuck/dip")
async def sell_stuck_dip():
    """Sell all stuck dip buyer bot tokens on-chain via Jupiter."""
    return await _sell_stuck_tokens("dip")


async def _sell_stuck_tokens(bot: str):
    """Actually sell tokens on-chain and close DB positions."""
    # Use bot-specific wallet key
    _wallet_key_map = {
        "meme": "TRADING_WALLET_PRIVATE_KEY",
        "copy": "COPY_TRADER_PRIVATE_KEY",
        "sniper": "SNIPER_WALLET_PRIVATE_KEY",
        "migration": "MIGRATION_WALLET_PRIVATE_KEY",
        "whale": "WHALE_FOLLOW_PRIVATE_KEY",
        "momentum": "MIGRATION_WALLET_PRIVATE_KEY",
        "dip": "MIGRATION_WALLET_PRIVATE_KEY",
        "convergence": "WHALE_FOLLOW_PRIVATE_KEY",
    }
    wallet_env = _wallet_key_map.get(bot, "TRADING_WALLET_PRIVATE_KEY")
    wallet_key = os.environ.get(wallet_env, "") or _TRADING_WALLET_KEY
    if not wallet_key:
        raise HTTPException(status_code=503, detail=f"No wallet key for {bot} ({wallet_env})")

    # Get wallet address
    try:
        from solders.keypair import Keypair
        import base58 as b58
        pk = wallet_key
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
    table = {"meme": "paper_positions", "copy": "copy_positions", "sniper": "snipe_positions", "migration": "migration_positions", "momentum": "momentum_positions", "whale": "whale_positions", "dip": "dip_positions", "convergence": "convergence_positions"}.get(bot)
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
  .badge-cyan { background: rgba(6,182,212,.15); color: #06b6d4; border: 1px solid rgba(6,182,212,.3); }
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
    <button onclick="sellStuck('migration')" class="danger" title="Sell all stuck migration tokens on-chain">&#9888; Sell Migration Tokens</button>
    <button onclick="sellStuck('convergence')" class="danger" title="Sell all stuck convergence tokens on-chain">&#9888; Sell Convergence Tokens</button>
    <button onclick="sellStuck('whale')" class="danger" title="Sell all stuck whale tokens on-chain">&#9888; Sell Whale Tokens</button>
    <button onclick="sellStuck('dip')" class="danger" title="Sell all stuck dip buyer tokens on-chain">&#9888; Sell Dip Tokens</button>
    <button onclick="refreshNow()">&#8635; REFRESH NOW</button>
  </div>

  <!-- Wallet P&L Overview -->
  <div class="card" style="margin-bottom:12px" id="wallet-pnl-card">
    <div class="card-header">&#128176; Total Wallet P&L</div>
    <div id="wallet-pnl-body">Loading...</div>
  </div>

  <!-- Bot summary cards -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px" id="bot-cards">
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
    <div class="card" id="migration-card">
      <div class="card-header">&#128640; Migration Sniper</div>
      <div class="card-body" id="migration-body">Loading...</div>
    </div>
    <div class="card" id="momentum-card">
      <div class="card-header">&#9889; Momentum Bot</div>
      <div class="card-body" id="momentum-body">Loading...</div>
    </div>
    <div class="card" id="whale-card">
      <div class="card-header">&#128011; Whale Shadow</div>
      <div class="card-body" id="whale-body">Loading...</div>
    </div>
    <div class="card" id="dip-card">
      <div class="card-header">&#128260; Dip Buyer</div>
      <div class="card-body" id="dip-body">Loading...</div>
    </div>
    <div class="card" id="convergence-card">
      <div class="card-header">&#127919; Convergence Bot</div>
      <div class="card-body" id="convergence-body">Loading...</div>
    </div>
  </div>

  <!-- Open positions -->
  <div class="grid-2">
    <div class="card">
      <div class="card-header">&#128200; Meme Open Positions</div>
      <div id="meme-positions">Loading...</div>
    </div>
    <div class="card">
      <div class="card-header">&#128200; Copy Bot Positions</div>
      <div id="copy-positions">Loading...</div>
    </div>
    <div class="card">
      <div class="card-header">&#127919; Sniper Bot Positions</div>
      <div id="sniper-positions">Loading...</div>
    </div>
    <div class="card">
      <div class="card-header">&#128640; Migration Sniper Positions</div>
      <div id="migration-positions">Loading...</div>
    </div>
    <div class="card">
      <div class="card-header">&#9889; Momentum Bot Positions</div>
      <div id="momentum-positions">Loading...</div>
    </div>
    <div class="card">
      <div class="card-header">&#128011; Whale Shadow Positions</div>
      <div id="whale-positions">Loading...</div>
    </div>
    <div class="card">
      <div class="card-header">&#128260; Dip Buyer Positions</div>
      <div id="dip-positions">Loading...</div>
    </div>
  </div>
  <div class="grid-2">
    <div class="card">
      <div class="card-header">&#127919; Convergence Positions</div>
      <div id="convergence-positions">Loading...</div>
    </div>
  </div>

  <!-- Whale Shadow Monitor -->
  <div class="card" style="margin-bottom:12px">
    <div class="card-header">&#128011; Whale Shadow Monitor</div>
    <div id="whale-monitor">Loading...</div>
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

  <!-- Wallet Configuration -->
  <div class="card" style="margin-bottom:12px">
    <div class="card-header">&#128273; Wallet Configuration &amp; Live Toggle</div>
    <div style="padding:12px" id="wallet-config">
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:16px">
        <div>
          <div class="section-title">Meme + Migration Bot</div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Wallet Address</label>
            <input type="text" id="wallet-meme-addr" placeholder="Public key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px" readonly>
          </div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Private Key</label>
            <input type="password" id="wallet-meme-key" placeholder="Paste private key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px">
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <button onclick="saveWallet('meme')" style="padding:4px 12px;font-size:11px">Save Key</button>
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" id="live-toggle-meme" onchange="toggleLive('meme', this.checked)">
              <span class="muted" style="font-size:11px">GO LIVE</span>
            </label>
          </div>
        </div>
        <div>
          <div class="section-title">Copy Bot</div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Wallet Address</label>
            <input type="text" id="wallet-copy-addr" placeholder="Public key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px" readonly>
          </div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Private Key</label>
            <input type="password" id="wallet-copy-key" placeholder="Paste private key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px">
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <button onclick="saveWallet('copy')" style="padding:4px 12px;font-size:11px">Save Key</button>
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" id="live-toggle-copy" onchange="toggleLive('copy', this.checked)">
              <span class="muted" style="font-size:11px">GO LIVE</span>
            </label>
          </div>
        </div>
        <div>
          <div class="section-title">Sniper Bot</div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Wallet Address</label>
            <input type="text" id="wallet-sniper-addr" placeholder="Public key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px" readonly>
          </div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Private Key</label>
            <input type="password" id="wallet-sniper-key" placeholder="Paste private key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px">
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <button onclick="saveWallet('sniper')" style="padding:4px 12px;font-size:11px">Save Key</button>
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" id="live-toggle-sniper" onchange="toggleLive('sniper', this.checked)">
              <span class="muted" style="font-size:11px">GO LIVE</span>
            </label>
          </div>
        </div>
        <div>
          <div class="section-title">Migration Sniper</div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Wallet Address</label>
            <input type="text" id="wallet-migration-addr" placeholder="Public key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px" readonly>
          </div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Private Key</label>
            <input type="password" id="wallet-migration-key" placeholder="Paste private key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px">
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <button onclick="saveWallet('migration')" style="padding:4px 12px;font-size:11px">Save Key</button>
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" id="live-toggle-migration" onchange="toggleLive('migration', this.checked)">
              <span class="muted" style="font-size:11px">GO LIVE</span>
            </label>
          </div>
        </div>
        <div>
          <div class="section-title">Momentum Breakout Bot</div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Wallet Address</label>
            <input type="text" id="wallet-momentum-addr" placeholder="Public key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px" readonly>
          </div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Private Key (MOMENTUM_WALLET_PRIVATE_KEY)</label>
            <input type="password" id="wallet-momentum-key" placeholder="Paste private key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px">
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <button onclick="saveWallet('momentum')" style="padding:4px 12px;font-size:11px">Save Key</button>
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" id="live-toggle-momentum" onchange="toggleLive('momentum', this.checked)">
              <span class="muted" style="font-size:11px">GO LIVE</span>
            </label>
          </div>
        </div>
        <div>
          <div class="section-title">&#128011; Whale Shadow Bot</div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Wallet Address</label>
            <input type="text" id="wallet-whale-addr" placeholder="Public key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px" readonly>
          </div>
          <div style="margin-bottom:6px">
            <label class="muted" style="font-size:11px">Private Key (WHALE_FOLLOW_PRIVATE_KEY)</label>
            <input type="password" id="wallet-whale-key" placeholder="Paste private key..." style="width:100%;padding:4px 8px;background:#1a1d2e;border:1px solid #2a2d3e;color:#e1e4e8;border-radius:4px;font-size:12px">
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <button onclick="saveWallet('whale')" style="padding:4px 12px;font-size:11px">Save Key</button>
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" id="live-toggle-whale" onchange="toggleLive('whale', this.checked)">
              <span class="muted" style="font-size:11px">GO LIVE</span>
            </label>
          </div>
        </div>
      </div>
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
  if (bot === 'MIGRATION') return '<span class="badge badge-red">MIGR</span>';
  if (bot === 'MOMENTUM') return '<span class="badge badge-green">MMTM</span>';
  if (bot === 'WHALE') return '<span class="badge badge-cyan">WHALE</span>';
  if (bot === 'DIP') return '<span class="badge badge-purple">DIP</span>';
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

  renderWalletPnl(d.wallet_pnl, d.meme, d.copy, d.sniper, d.migration);
  renderMemeCard(d.meme);
  renderCopyCard(d.copy);
  renderSniperCard(d.sniper);
  renderMigrationCard(d.migration);
  renderMomentumCard(d.momentum);
  renderWhaleCard(d.whale);
  renderDipCard(d.dip_buyer);
  renderConvergenceCard(d.convergence);
  renderMemePositions(d.meme.open_positions);
  renderCopyPositions(d.copy.open_positions);
  renderSniperPositions(d.sniper.open_positions);
  renderMigrationPositions(d.migration.open_positions);
  renderMomentumPositions(d.momentum.open_positions);
  renderWhalePositions(d.whale.open_positions);
  renderWhaleMonitor(d.whale);
  renderDipPositions(d.dip_buyer.open_positions);
  renderConvergencePositions(d.convergence.open_positions);
  renderRecentTrades(d.recent_trades);
  renderConfigs(d.meme.config, d.copy.config, d.sniper.config, d.migration.config, d.momentum.config, d.whale.config, d.dip_buyer.config);
  updateWalletUI(d);
}

function renderWalletPnl(w, meme, copy, sniper, migration) {
  const totalSol = w.total_sol_balance;
  const memeOpen = (meme.open_positions || []).length;
  const copyOpen = (copy.open_positions || []).length;
  const sniperOpen = (sniper.open_positions || []).length;
  const migOpen = (migration.open_positions || []).length;
  const totalOpen = memeOpen + copyOpen + sniperOpen + migOpen;

  const memeNet = w.meme_realized_usd;
  const memeNetCls = memeNet >= 0 ? 'green' : 'red';

  const sWinRate = w.sniper_closed > 0 ? (w.sniper_wins / w.sniper_closed * 100).toFixed(1) : '—';
  const cWinRate = w.copy_closed > 0 ? (w.copy_wins / w.copy_closed * 100).toFixed(1) : '—';
  const mWinRate = meme.total_trades > 0 ? meme.win_rate_pct.toFixed(1) : '—';

  document.getElementById('wallet-pnl-body').innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;padding:12px">
      <div>
        <div class="muted" style="font-size:11px">TOTAL SOL</div>
        <div style="font-size:20px;font-weight:bold">${totalSol.toFixed(4)} SOL</div>
        <div class="muted" style="font-size:11px;margin-top:4px">
          Meme: ${w.meme_wallet_sol.toFixed(4)} | Copy: ${w.copy_wallet_sol.toFixed(4)} | Snipe: ${w.sniper_wallet_sol.toFixed(4)}
        </div>
      </div>
      <div>
        <div class="muted" style="font-size:11px">OPEN POSITIONS</div>
        <div style="font-size:20px;font-weight:bold">${totalOpen}</div>
        <div class="muted" style="font-size:11px;margin-top:4px">
          Meme: ${memeOpen} | Copy: ${copyOpen} | Snipe: ${sniperOpen} | Mig: ${migOpen}
        </div>
      </div>
      <div>
        <div class="muted" style="font-size:11px">MEME REALIZED P&L</div>
        <div style="font-size:20px;font-weight:bold" class="${memeNetCls}">$${Math.abs(memeNet).toFixed(2)}</div>
        <div class="muted" style="font-size:11px;margin-top:4px">
          Win rate: ${mWinRate}% (${meme.winners || 0}W / ${meme.losers || 0}L)
        </div>
      </div>
      <div>
        <div class="muted" style="font-size:11px">SNIPER / COPY STATS</div>
        <div style="font-size:13px;margin-top:4px">
          <span class="yellow">Sniper:</span> ${w.sniper_closed} trades, ${sWinRate}% WR, avg ${w.sniper_avg_pnl >= 0 ? '+' : ''}${w.sniper_avg_pnl.toFixed(1)}%
        </div>
        <div style="font-size:13px;margin-top:2px">
          <span class="blue">Copy:</span> ${w.copy_closed} trades, ${cWinRate}% WR, avg ${w.copy_avg_pnl >= 0 ? '+' : ''}${w.copy_avg_pnl.toFixed(1)}%
        </div>
      </div>
    </div>`;
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

function renderMigrationCard(m) {
  const winRate = m.closed > 0 ? (m.wins / m.closed * 100).toFixed(1) : '—';
  document.getElementById('migration-body').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Mode</span>
      <span class="stat-value">${m.mode === 'paper'
        ? '<span class="badge badge-yellow">PAPER</span>'
        : '<span class="badge badge-red">LIVE</span>'}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Trades</span>
      <span class="stat-value">${m.total_trades} (${m.wins}W / ${m.losses}L)</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win Rate</span>
      <span class="stat-value">${winRate}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Avg PnL</span>
      <span class="stat-value ${m.avg_pnl >= 0 ? 'green' : 'red'}">${m.avg_pnl >= 0 ? '+' : ''}${m.avg_pnl.toFixed(1)}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Curve Target</span>
      <span class="stat-value">${m.config.min_curve_pct || 90}% – ${m.config.max_curve_pct || 99}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">SL / TP1 / TP2</span>
      <span class="stat-value">${m.config.stop_loss_pct || 25}% / ${m.config.tp1_pct || 100}% / ${m.config.tp2_pct || 200}%</span>
    </div>
  `;
}

function renderMomentumCard(m) {
  if (!m) return;
  const winRate = m.closed > 0 ? (m.wins / m.closed * 100).toFixed(1) : '—';
  const mcMin = m.config.min_mc_usd ? fmtMc(m.config.min_mc_usd) : '—';
  const mcMax = m.config.max_mc_usd ? fmtMc(m.config.max_mc_usd) : '—';
  document.getElementById('momentum-body').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Mode</span>
      <span class="stat-value">${m.mode === 'paper'
        ? '<span class="badge badge-yellow">PAPER</span>'
        : '<span class="badge badge-red">LIVE</span>'}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Total Trades</span>
      <span class="stat-value">${m.total_trades}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win / Loss</span>
      <span class="stat-value"><span class="green">${m.wins}W</span> / <span class="red">${m.losses}L</span></span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Avg PnL</span>
      <span class="stat-value ${m.avg_pnl >= 0 ? 'green' : 'red'}">${m.avg_pnl >= 0 ? '+' : ''}${m.avg_pnl.toFixed(1)}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">MC Range</span>
      <span class="stat-value">${mcMin} – ${mcMax}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">SL / TP1 / TP2</span>
      <span class="stat-value">${m.config.stop_loss_pct || 8}% / ${m.config.tp1_pct || 15}% / ${m.config.tp2_pct || 30}%</span>
    </div>
    ${m.wallet_pubkey ? `<div class="wallet-addr">${m.wallet_pubkey.slice(0,12)}...${m.wallet_pubkey.slice(-8)}</div>` : '<div class="wallet-addr muted">No wallet configured</div>'}
  `;
}

function renderMomentumPositions(positions) {
  if (!positions || positions.length === 0) {
    document.getElementById('momentum-positions').innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const rows = positions.map(p => {
    const pnl = Number(p.pnl_pct || 0);
    const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
    const sym = p.symbol || '???';
    const addr = p.mint || '';
    const age = timeSince(p.opened_at);
    const m5 = p.entry_m5_pct !== null && p.entry_m5_pct !== undefined ? '+' + Number(p.entry_m5_pct).toFixed(1) + '%' : '—';
    const bsRatio = p.entry_buy_sell_ratio !== null && p.entry_buy_sell_ratio !== undefined ? Number(p.entry_buy_sell_ratio).toFixed(2) : '—';
    return `<tr class="${rowCls}">
      <td><a class="link" href="https://dexscreener.com/solana/${addr}" target="_blank">$${sym}</a></td>
      <td>${fmtPnl(pnl)}</td>
      <td>${fmtSol(p.position_size_sol)}</td>
      <td>${m5}</td>
      <td>${bsRatio}</td>
      <td class="muted">${age}</td>
    </tr>`;
  }).join('');
  document.getElementById('momentum-positions').innerHTML = `
    <table>
      <thead><tr>
        <th>Token</th><th>PnL%</th><th>Size</th><th>Entry 5m%</th><th>Buy Ratio</th><th>Age</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderWhaleCard(w) {
  if (!w) return;
  const winRate = w.closed > 0 ? (w.wins / w.closed * 100).toFixed(1) : '—';
  document.getElementById('whale-body').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Mode</span>
      <span class="stat-value">${w.mode === 'paper'
        ? '<span class="badge badge-yellow">PAPER</span>'
        : '<span class="badge badge-red">LIVE</span>'}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Tracked Whales</span>
      <span class="stat-value">${w.tracked_wallets}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Total Trades</span>
      <span class="stat-value">${w.total_trades}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win / Loss</span>
      <span class="stat-value"><span class="green">${w.wins}W</span> / <span class="red">${w.losses}L</span></span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win Rate</span>
      <span class="stat-value">${winRate}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Avg PnL</span>
      <span class="stat-value ${w.avg_pnl >= 0 ? 'green' : 'red'}">${w.avg_pnl >= 0 ? '+' : ''}${w.avg_pnl.toFixed(1)}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Min Buy $</span>
      <span class="stat-value">${fmtUsd(w.config.min_buy_usd || 900)}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Trail / Stop</span>
      <span class="stat-value">${w.config.trailing_stop_pct || 25}% / ${w.config.hard_stop_pct || 25}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Open Positions</span>
      <span class="stat-value">${(w.open_positions || []).length} / ${w.config.max_open_positions || 8}</span>
    </div>
    ${w.wallet_pubkey ? `<div class="wallet-addr">${w.wallet_pubkey.slice(0,12)}...${w.wallet_pubkey.slice(-8)}</div>` : '<div class="wallet-addr muted">No wallet configured</div>'}
  `;
}

function renderWhalePositions(positions) {
  if (!positions || positions.length === 0) {
    document.getElementById('whale-positions').innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const rows = positions.map(p => {
    const pnl = Number(p.pnl_pct || 0);
    const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
    const sym = p.token_symbol || '???';
    const addr = p.token_address || '';
    const age = timeSince(p.opened_at);
    const whaleSuffix = (p.whale_wallet || '').slice(0, 8);
    const whaleBuy = p.whale_buy_usd ? fmtUsd(p.whale_buy_usd) : '—';
    return `<tr class="${rowCls}">
      <td><a class="link" href="https://dexscreener.com/solana/${addr}" target="_blank">$${sym}</a></td>
      <td>${fmtPnl(pnl)}</td>
      <td>${fmtSol(p.position_size_sol)}</td>
      <td>${whaleBuy}</td>
      <td class="muted" title="${p.whale_wallet || ''}">${whaleSuffix}...</td>
      <td class="muted">${age}</td>
    </tr>`;
  }).join('');
  document.getElementById('whale-positions').innerHTML = `
    <table>
      <thead><tr>
        <th>Token</th><th>PnL%</th><th>Size</th><th>Whale Buy $</th><th>Whale Addr</th><th>Age</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
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

function renderCopyPositions(positions) {
  if (!positions || positions.length === 0) {
    document.getElementById('copy-positions').innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const rows = positions.map(p => {
    const pnl = Number(p.pnl_pct || 0);
    const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
    const sym = p.token_symbol || '???';
    const addr = p.token_address || '';
    const age = timeSince(p.opened_at);
    const wallet = (p.copied_wallet || '').slice(0, 6) + '..';
    const label = p.wallet_label || wallet;
    return `<tr class="${rowCls}">
      <td><a class="link" href="https://dexscreener.com/solana/${addr}" target="_blank">$${sym}</a></td>
      <td>${fmtPnl(pnl)}</td>
      <td>${p.position_size_sol ? fmtSol(p.position_size_sol) : p.position_size_usd ? fmtUsd(p.position_size_usd) : '—'}</td>
      <td>${p.entry_price ? '$' + Number(p.entry_price).toFixed(6) : '—'}</td>
      <td>${p.current_price ? '$' + Number(p.current_price).toFixed(6) : '—'}</td>
      <td class="muted" title="${p.copied_wallet || ''}">${label}</td>
      <td class="muted">${age}</td>
    </tr>`;
  }).join('');
  document.getElementById('copy-positions').innerHTML = `
    <table>
      <thead><tr>
        <th>Token</th><th>PnL%</th><th>Size</th><th>Entry</th><th>Current</th><th>Wallet</th><th>Age</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderSniperPositions(positions) {
  if (!positions || positions.length === 0) {
    document.getElementById('sniper-positions').innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const rows = positions.map(p => {
    const pnl = Number(p.pnl_pct || 0);
    const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
    const sym = p.symbol || '???';
    const addr = p.mint || '';
    const age = timeSince(p.opened_at);
    const curve = p.entry_curve_pct ? Number(p.entry_curve_pct).toFixed(0) + '%' : '—';
    const rem = p.remaining_pct !== null && p.remaining_pct !== undefined ? Number(p.remaining_pct).toFixed(0) + '%' : '100%';
    const grad = p.graduated ? '<span class="green">&#10003;</span>' : '';
    return `<tr class="${rowCls}">
      <td><a class="link" href="https://pump.fun/${addr}" target="_blank">$${sym}</a></td>
      <td>${fmtPnl(pnl)}</td>
      <td>${fmtSol(p.position_size_sol)}</td>
      <td>${curve}</td>
      <td>${rem}</td>
      <td>${p.entry_holders || '—'}</td>
      <td>${grad}</td>
      <td class="muted">${age}</td>
    </tr>`;
  }).join('');
  document.getElementById('sniper-positions').innerHTML = `
    <table>
      <thead><tr>
        <th>Token</th><th>PnL%</th><th>Size</th><th>Entry Curve</th><th>Rem%</th><th>Holders</th><th>Grad</th><th>Age</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderMigrationPositions(positions) {
  if (!positions || positions.length === 0) {
    document.getElementById('migration-positions').innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const rows = positions.map(p => {
    const pnl = Number(p.pnl_pct || 0);
    const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
    const sym = p.symbol || '???';
    const addr = p.mint || '';
    const age = timeSince(p.opened_at);
    const curve = p.entry_curve_pct ? Number(p.entry_curve_pct).toFixed(0) + '%' : '—';
    const rem = p.remaining_pct !== null && p.remaining_pct !== undefined ? Number(p.remaining_pct).toFixed(0) + '%' : '100%';
    const mig = p.migrated ? '<span class="green">RAYDIUM</span>' : '<span class="yellow">CURVE</span>';
    const peak = p.highest_price_sol && p.entry_price_sol ? (Number(p.highest_price_sol) / Number(p.entry_price_sol)).toFixed(1) + 'x' : '—';
    return `<tr class="${rowCls}">
      <td><a class="link" href="https://pump.fun/${addr}" target="_blank">$${sym}</a></td>
      <td>${fmtPnl(pnl)}</td>
      <td>${fmtSol(p.position_size_sol)}</td>
      <td>${curve}</td>
      <td>${rem}</td>
      <td>${peak}</td>
      <td>${mig}</td>
      <td class="muted">${age}</td>
    </tr>`;
  }).join('');
  document.getElementById('migration-positions').innerHTML = `
    <table>
      <thead><tr>
        <th>Token</th><th>PnL%</th><th>Size</th><th>Entry Curve</th><th>Rem%</th><th>Peak</th><th>Status</th><th>Age</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderDipCard(d) {
  if (!d) return;
  const winRate = d.closed > 0 ? (d.wins / d.closed * 100).toFixed(1) : '—';
  document.getElementById('dip-body').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Mode</span>
      <span class="stat-value">${d.mode === 'paper'
        ? '<span class="badge badge-yellow">PAPER</span>'
        : '<span class="badge badge-red">LIVE</span>'}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Total Trades</span>
      <span class="stat-value">${d.total_trades}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win / Loss</span>
      <span class="stat-value"><span class="green">${d.wins}W</span> / <span class="red">${d.losses}L</span></span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win Rate</span>
      <span class="stat-value">${winRate}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Avg PnL</span>
      <span class="stat-value ${d.avg_pnl >= 0 ? 'green' : 'red'}">${d.avg_pnl >= 0 ? '+' : ''}${d.avg_pnl.toFixed(1)}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Reversal %</span>
      <span class="stat-value">${d.config.reversal_pct || 5}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Dump Range</span>
      <span class="stat-value">${d.config.min_dump_pct || 5}% – ${d.config.max_dump_pct || 60}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">TP1 / TP2</span>
      <span class="stat-value">${d.config.tp1_pct || 100}% / ${d.config.tp2_pct || 200}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Trail / Stop</span>
      <span class="stat-value">${d.config.trailing_stop_pct || 25}% / ${d.config.hard_stop_pct || 25}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Open Positions</span>
      <span class="stat-value">${(d.open_positions || []).length} / ${d.config.max_open_positions || 5}</span>
    </div>
  `;
}

function renderConvergenceCard(d) {
  if (!d) { document.getElementById('convergence-body').innerHTML = '<div class="empty">No data</div>'; return; }
  const winRate = d.closed > 0 ? (d.wins / d.closed * 100).toFixed(1) : '—';
  document.getElementById('convergence-body').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Mode</span>
      <span class="stat-value">${d.mode === 'paper'
        ? '<span class="badge badge-yellow">PAPER</span>'
        : '<span class="badge badge-red">LIVE</span>'}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Total Trades</span>
      <span class="stat-value">${d.total_trades}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win / Loss</span>
      <span class="stat-value"><span class="green">${d.wins}W</span> / <span class="red">${d.losses}L</span></span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Win Rate</span>
      <span class="stat-value">${winRate}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Avg PnL</span>
      <span class="stat-value ${d.avg_pnl >= 0 ? 'green' : 'red'}">${d.avg_pnl >= 0 ? '+' : ''}${d.avg_pnl.toFixed(1)}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Min Sources</span>
      <span class="stat-value">${d.config.min_convergence_sources || 3}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">MC Range</span>
      <span class="stat-value">$${((d.config.min_mc_usd||100000)/1000).toFixed(0)}K – $${((d.config.max_mc_usd||5000000)/1000000).toFixed(0)}M</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">TP1 / TP2</span>
      <span class="stat-value">${d.config.tp1_pct || 20}% / ${d.config.tp2_pct || 50}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Trail / Stop</span>
      <span class="stat-value">${d.config.trailing_distance_pct || 15}% / ${d.config.hard_stop_pct || 15}%</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Open Positions</span>
      <span class="stat-value">${(d.open_positions || []).length} / ${d.config.max_open_positions || 3}</span>
    </div>
  `;
}

function renderDipPositions(positions) {
  if (!positions || positions.length === 0) {
    document.getElementById('dip-positions').innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const rows = positions.map(p => {
    const pnl = Number(p.pnl_pct || 0);
    const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
    const sym = p.token_symbol || '???';
    const addr = p.token_address || '';
    const age = timeSince(p.opened_at);
    const dump = p.dump_pct !== null && p.dump_pct !== undefined ? '-' + Number(p.dump_pct).toFixed(1) + '%' : '—';
    const entry = p.entry_price ? '$' + Number(p.entry_price).toFixed(8) : '—';
    const rem = p.remaining_pct !== null && p.remaining_pct !== undefined ? Number(p.remaining_pct).toFixed(0) + '%' : '100%';
    const size = p.position_size_sol ? fmtSol(p.position_size_sol) : '—';
    return `<tr class="${rowCls}">
      <td><a class="link" href="https://dexscreener.com/solana/${addr}" target="_blank">$${sym}</a></td>
      <td>${fmtPnl(pnl)}</td>
      <td>${size}</td>
      <td>${dump}</td>
      <td>${entry}</td>
      <td>${rem}</td>
      <td class="muted">${age}</td>
    </tr>`;
  }).join('');
  document.getElementById('dip-positions').innerHTML = `
    <table>
      <thead><tr>
        <th>Token</th><th>PnL%</th><th>Size</th><th>Dump%</th><th>Entry</th><th>Rem%</th><th>Age</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderConvergencePositions(positions) {
  if (!positions || positions.length === 0) {
    document.getElementById('convergence-positions').innerHTML = '<div class="empty">No open positions</div>';
    return;
  }
  const rows = positions.map(p => {
    const sym = p.token_symbol || '?';
    const addr = p.token_address || '';
    const pnl = p.pnl_pct || 0;
    const size = (p.position_size_sol || 0).toFixed(2);
    const rem = (p.remaining_pct || 100).toFixed(0);
    const sources = p.signal_sources ? JSON.parse(p.signal_sources).join(', ') : '?';
    const age = p.opened_at ? Math.round((Date.now() - new Date(p.opened_at + 'Z').getTime()) / 60000) + 'min' : '?';
    const rowCls = pnl > 0 ? '' : 'neg';
    return `<tr class="${rowCls}">
      <td><a class="link" href="https://dexscreener.com/solana/${addr}" target="_blank">$${sym}</a></td>
      <td>${fmtPnl(pnl)}</td>
      <td>${size}</td>
      <td>${rem}%</td>
      <td class="muted" style="font-size:10px">${sources}</td>
      <td class="muted">${age}</td>
    </tr>`;
  }).join('');
  document.getElementById('convergence-positions').innerHTML = `
    <table>
      <thead><tr>
        <th>Token</th><th>PnL%</th><th>Size</th><th>Rem</th><th>Sources</th><th>Age</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderWhaleMonitor(w) {
  if (!w) { document.getElementById('whale-monitor').innerHTML = '<div class="empty">No whale data</div>'; return; }
  const openPositions = w.open_positions || [];
  const closedRecent = w.closed_recent || [];

  let openHtml = '';
  if (openPositions.length === 0) {
    openHtml = '<div class="empty">No open whale positions</div>';
  } else {
    const rows = openPositions.map(p => {
      const sym = p.token_symbol || '???';
      const addr = p.token_address || '';
      const entryMc = Number(p.entry_mc || 0);
      const curMc = Number(p.current_mc || 0);
      const sizeSol = Number(p.position_size_sol || 0);
      // Calculate PnL from MC change (more reliable than entry_price which may be dummy)
      let pnl = Number(p.pnl_pct || 0);
      let curVal = sizeSol;
      if (entryMc > 0 && curMc > 0) {
        pnl = ((curMc - entryMc) / entryMc * 100);
        curVal = sizeSol * (1 + pnl / 100);
      } else if (pnl !== 0) {
        curVal = sizeSol * (1 + pnl / 100);
      }
      const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
      const age = timeSince(p.opened_at);
      return `<tr class="${rowCls}">
        <td><a class="link" href="https://dexscreener.com/solana/${addr}" target="_blank">$${sym}</a></td>
        <td>${entryMc > 0 ? fmtMc(entryMc) : '<span class="muted">--</span>'}</td>
        <td>${curMc > 0 ? fmtMc(curMc) : '<span class="muted">--</span>'}</td>
        <td>${fmtSol(sizeSol)}</td>
        <td>${curVal > 0 ? curVal.toFixed(4) : sizeSol.toFixed(4)} SOL</td>
        <td>${entryMc > 0 && curMc > 0 ? fmtPnl(pnl) : '<span class="muted">--</span>'}</td>
        <td class="muted">${age}</td>
        <td><button class="danger" style="padding:3px 8px;font-size:10px" onclick="sellWhaleToken(${p.id}, '${sym}')">SELL NOW</button></td>
      </tr>`;
    }).join('');
    openHtml = `<table>
      <thead><tr>
        <th>Token</th><th>Entry MC</th><th>Current MC</th><th>Size</th><th>Value</th><th>PnL%</th><th>Age</th><th>Action</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }

  let closedHtml = '';
  if (closedRecent.length === 0) {
    closedHtml = '<div class="empty">No recent closed trades</div>';
  } else {
    const cRows = closedRecent.map(p => {
      const sym = p.token_symbol || '???';
      const addr = p.token_address || '';
      const entryMc = Number(p.entry_mc || 0);
      const entryPrice = Number(p.entry_price || 0);
      const exitPrice = Number(p.current_price || 0);
      // Calculate exit MC from price ratio, or show -- if bad data
      const exitMc = entryPrice > 0.00001 && entryMc > 0 ? (exitPrice / entryPrice) * entryMc : 0;
      // PnL from MC if available, else from DB
      let pnl = Number(p.pnl_pct || 0);
      if (entryMc > 0 && exitMc > 0 && entryPrice > 0.00001) {
        pnl = ((exitMc - entryMc) / entryMc * 100);
      }
      const rowCls = pnl > 0 ? 'pos-row-green' : pnl < -10 ? 'pos-row-red' : '';
      const reason = p.close_reason || '—';
      let holdTime = '—';
      if (p.opened_at && p.closed_at) {
        const opened = new Date(p.opened_at + (p.opened_at.endsWith('Z') ? '' : 'Z'));
        const closed = new Date(p.closed_at + (p.closed_at.endsWith('Z') ? '' : 'Z'));
        const diffS = Math.floor((closed - opened) / 1000);
        if (diffS < 60) holdTime = diffS + 's';
        else if (diffS < 3600) holdTime = Math.floor(diffS / 60) + 'm';
        else holdTime = Math.floor(diffS / 3600) + 'h ' + Math.floor((diffS % 3600) / 60) + 'm';
      }
      const hasBadEntry = entryPrice < 0.00001 || entryPrice > 50;
      return `<tr class="${rowCls}">
        <td><a class="link" href="https://dexscreener.com/solana/${addr}" target="_blank">$${sym}</a></td>
        <td>${entryMc > 0 ? fmtMc(entryMc) : '<span class="muted">—</span>'}</td>
        <td>${exitMc > 0 && !hasBadEntry ? fmtMc(exitMc) : '<span class="muted">—</span>'}</td>
        <td>${!hasBadEntry ? fmtPnl(pnl) : '<span class="muted">—</span>'}</td>
        <td class="muted" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${reason}</td>
        <td class="muted">${holdTime}</td>
      </tr>`;
    }).join('');
    closedHtml = `<table>
      <thead><tr>
        <th>Token</th><th>Entry MC</th><th>Exit MC</th><th>PnL%</th><th>Close Reason</th><th>Hold Time</th>
      </tr></thead>
      <tbody>${cRows}</tbody>
    </table>`;
  }

  document.getElementById('whale-monitor').innerHTML = `
    <div style="padding:8px 14px 4px"><div class="section-title" style="margin-top:0">Open Positions</div></div>
    ${openHtml}
    <div style="padding:8px 14px 4px"><div class="section-title" style="margin-top:0">Recent Closed Trades (Last 10)</div></div>
    ${closedHtml}
  `;
}

async function sellWhaleToken(posId, sym) {
  if (!confirm('Sell whale position $' + sym + ' (ID ' + posId + ') on-chain via Jupiter?')) return;
  showStatus('Selling whale token $' + sym + '... please wait');
  try {
    const r = await fetch('/control/sell-whale-token/' + posId, {method: 'POST'});
    const j = await r.json();
    if (r.ok) {
      const msg = j.sold_onchain ? 'Sold on-chain (tx: ' + j.tx_hash + ')' : 'DB closed' + (j.error ? ' (on-chain: ' + j.error + ')' : '');
      showStatus('$' + sym + ': ' + msg);
    } else {
      showStatus('Error: ' + (j.detail || 'unknown'), true);
    }
    await fetchStatus();
  } catch(e) { showStatus('Error: ' + e.message, true); }
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
  const endpoint = bot === 'meme' ? '/config' : bot === 'copy' ? '/config/copy' : bot === 'migration' ? '/config/migration' : bot === 'momentum' ? '/config/momentum' : bot === 'whale' ? '/config/whale' : bot === 'dip' ? '/config/dip' : '/config/sniper';
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

function renderConfigs(mCfg, cCfg, sCfg, migCfg, momCfg, whaleCfg, dipCfg) {
  const mKeys = ['base_position_sol','max_open_positions','daily_loss_limit_sol','stop_loss_pct',
                  'trailing_stop_activation_pct','trailing_stop_trail_pct','max_holding_hours',
                  'min_mc','max_mc','min_agiotage_score','min_sources','min_volume_h1',
                  'buy_slippage_bps','sell_slippage_bps','ratchet_stop_enabled','ratchet_stop_pct_of_tp'];
  document.getElementById('meme-config').innerHTML =
    `<div class="config-grid">${mKeys.map(k => editItem('meme', k, mCfg[k])).join('')}</div>`;

  const cKeys = ['position_size_sol','max_open_positions','daily_loss_limit_sol',
                  'emergency_exit_pct','emergency_exit_seconds',
                  'stop_loss_pct','tp1_pct','tp1_sell_pct','tp2_pct','tp2_sell_pct',
                  'trailing_activate_pct','trailing_distance_pct',
                  'max_hold_hours','min_wallet_winrate','paper_mode','enabled'];
  const sKeys = ['position_size_sol','max_open_positions','daily_loss_limit_sol',
                  'emergency_exit_pct','emergency_exit_seconds',
                  'stop_loss_pct','tp1_pct','tp1_sell_pct','tp2_pct','tp2_sell_pct',
                  'trailing_activate_pct','trailing_distance_pct',
                  'stagnation_seconds','min_curve_pct','max_curve_pct',
                  'min_holders','max_holders','min_buy_sell_ratio',
                  'max_hold_minutes','paper_mode','enabled'];
  const migKeys = ['position_size_sol','max_open_positions','daily_loss_limit_sol',
                    'graduation_real_sol','min_real_sol','max_real_sol',
                    'emergency_exit_pct','emergency_exit_seconds',
                    'migration_sell_pct','migration_wait_seconds',
                    'stop_loss_pct','trailing_distance_pct','stagnation_seconds',
                    'min_holders','min_volume_sol',
                    'max_hold_minutes','paper_mode','enabled'];
  const momKeys = ['position_size_sol','max_open_positions','daily_loss_limit_sol',
                    'min_m5_pct','min_buy_sell_ratio_m5',
                    'min_mc_usd','max_mc_usd','min_liquidity_usd',
                    'tp1_pct','tp1_sell_pct','tp2_pct',
                    'stop_loss_pct','trailing_distance_pct',
                    'stagnation_seconds','max_hold_seconds',
                    'paper_mode','enabled'];
  const whaleKeys = ['min_buy_usd','size_900','size_2000','size_5000',
                     'max_open_positions','daily_loss_limit_sol',
                     'trailing_stop_pct','hard_stop_pct',
                     'min_mc_usd','max_mc_usd','cooldown_hours',
                     'paper_mode','enabled'];
  const dipKeys = ['enabled', 'paper_mode',
                   'position_size_sol', 'max_open_positions', 'daily_loss_limit_sol',
                   'reversal_pct', 'min_dump_pct', 'max_dump_pct', 'max_wait_seconds',
                   'tp1_pct', 'tp1_sell_pct', 'tp2_pct', 'tp2_sell_pct',
                   'trailing_stop_pct', 'hard_stop_pct'];
  document.getElementById('other-config').innerHTML = `
    <div class="section-title">Copy Bot</div>
    <div class="config-grid">${cKeys.map(k => editItem('copy', k, cCfg[k])).join('')}</div>
    <div class="section-title" style="margin-top:12px">Sniper Bot</div>
    <div class="config-grid">${sKeys.map(k => editItem('sniper', k, sCfg[k])).join('')}</div>
    <div class="section-title" style="margin-top:12px">Migration Sniper</div>
    <div class="config-grid">${migKeys.map(k => editItem('migration', k, migCfg[k])).join('')}</div>
    <div class="section-title" style="margin-top:12px">Momentum Breakout Bot</div>
    <div class="config-grid">${momCfg ? momKeys.map(k => editItem('momentum', k, momCfg[k])).join('') : '<span class="muted">No config</span>'}</div>
    <div class="section-title" style="margin-top:12px">&#128011; Whale Shadow Bot</div>
    <div class="config-grid">${whaleCfg ? whaleKeys.map(k => editItem('whale', k, whaleCfg[k])).join('') : '<span class="muted">No config</span>'}</div>
    <div class="section-title" style="margin-top:12px">&#128260; Dip Buyer Bot</div>
    <div class="config-grid">${dipCfg ? dipKeys.map(k => editItem('dip', k, dipCfg[k])).join('') : '<span class="muted">No config</span>'}</div>
  `;
}

function updateWalletUI(d) {
  const w = d.wallets || {};
  for (const bot of ['meme', 'copy', 'sniper', 'migration', 'momentum', 'whale']) {
    const info = w[bot] || {};
    const addrEl = document.getElementById(`wallet-${bot}-addr`);
    if (addrEl) addrEl.value = info.pubkey || 'Not configured';
    const toggleEl = document.getElementById(`live-toggle-${bot}`);
    if (toggleEl) toggleEl.checked = !info.paper_mode;
  }
}

async function saveWallet(bot) {
  const keyEl = document.getElementById(`wallet-${bot}-key`);
  const key = keyEl ? keyEl.value.trim() : '';
  if (!key) { showStatus('No key entered', true); return; }
  if (!confirm(`Save private key for ${bot} bot? This writes to .env.local.`)) return;
  try {
    const r = await fetch('/wallet/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({bot, private_key: key})
    });
    const j = await r.json();
    if (r.ok) {
      showStatus(`${bot} wallet saved: ${j.pubkey}`);
      keyEl.value = '';
      await fetchStatus();
    } else {
      showStatus('Error: ' + (j.detail || 'unknown'), true);
    }
  } catch(e) { showStatus('Error: ' + e.message, true); }
}

async function toggleLive(bot, live) {
  const action = live ? 'GO LIVE' : 'switch to PAPER';
  if (live && !confirm(`${action} for ${bot} bot? This will execute REAL trades with REAL SOL.`)) {
    document.getElementById(`live-toggle-${bot}`).checked = false;
    return;
  }
  try {
    const r = await fetch('/wallet/toggle-live', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({bot, live})
    });
    const j = await r.json();
    if (r.ok) {
      showStatus(`${bot} bot: ${j.mode} mode`);
      await fetchStatus();
    } else {
      showStatus('Error: ' + (j.detail || 'unknown'), true);
      document.getElementById(`live-toggle-${bot}`).checked = !live;
    }
  } catch(e) {
    showStatus('Error: ' + e.message, true);
    document.getElementById(`live-toggle-${bot}`).checked = !live;
  }
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

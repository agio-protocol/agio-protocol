# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Convergence Bot — fires ONLY when multiple independent signals converge on the
same token within a 60-second window.

Sources:
1. cluster_signal  — smart money wallet clustering (from meme bot's DB)
2. momentum        — DexScreener 5m price action + volume
3. gmgn_smart      — GMGN smart money buys
4. gmgn_kol        — GMGN KOL (key opinion leader) buys
5. graduation      — pump.fun bonding curve completion (Redis pub/sub)

When 3+ distinct sources fire on the same token, we enter. Each source alone has
30-40% win rate. Together they indicate overwhelming consensus.

Exit: ratcheting TPs (+20% sell 34%, +50% sell 33%, trail 15% from peak), hard stop -15%.
"""
import asyncio
import json as _json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, BigInteger, Numeric, Boolean, DateTime, Text, Integer, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("convergence-bot")

WALLET_KEY_ENV = "WHALE_FOLLOW_PRIVATE_KEY"
SOL_MINT = "So11111111111111111111111111111111111111112"


# === CONFIG ===

DEFAULT_CONFIG = {
    "enabled": True,
    "paper_mode": True,

    "position_size_sol": 0.15,
    "max_open_positions": 3,
    "daily_loss_limit_sol": 0.50,

    "min_convergence_sources": 3,
    "convergence_window_seconds": 60,

    "min_mc_usd": 100000,
    "max_mc_usd": 5000000,
    "min_liquidity_usd": 10000,

    "tp1_pct": 20,
    "tp1_sell_pct": 34,
    "tp2_pct": 50,
    "tp2_sell_pct": 33,
    "trailing_activate_pct": 20,
    "trailing_distance_pct": 15,
    "hard_stop_pct": 15,

    "cooldown_hours": 4,
    "slippage_bps": 300,
    "scan_interval_seconds": 5,
}


async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("convergence_bot_config")
        if stored:
            return {**DEFAULT_CONFIG, **_json.loads(stored)}
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


# === DB MODELS ===

class ConvergencePosition(Base):
    __tablename__ = "convergence_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    entry_price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    entry_mc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    position_size_sol: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    remaining_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    tp1_done: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    close_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    signal_sources: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tx_hash_buy: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_conv_status", "status"),
        Index("idx_conv_token", "token_address"),
    )


class ConvergenceTrade(Base):
    __tablename__ = "convergence_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    amount_sol: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# === EXECUTION ===

def _get_keypair():
    from solders.keypair import Keypair
    import base58 as b58
    pk = os.getenv(WALLET_KEY_ENV, "")
    if not pk:
        raise ValueError(f"{WALLET_KEY_ENV} not set")
    if pk.startswith("["):
        return Keypair.from_bytes(bytes(_json.loads(pk)))
    return Keypair.from_bytes(b58.b58decode(pk))


async def _execute_buy(token_mint: str, amount_sol: float, slippage_bps: int = 300) -> dict:
    try:
        keypair = _get_keypair()
        jupiter = "https://api.jup.ag/swap/v1"
        _hk = os.getenv("HELIUS_API_KEY", "")
        rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

        async with httpx.AsyncClient() as client:
            qr = await client.get(f"{jupiter}/quote", params={
                "inputMint": SOL_MINT, "outputMint": token_mint,
                "amount": str(int(amount_sol * 1e9)), "slippageBps": str(slippage_bps),
            }, timeout=10)
            if qr.status_code != 200:
                return {"success": False, "error": f"Quote {qr.status_code}"}

            sr = await client.post(f"{jupiter}/swap", json={
                "quoteResponse": qr.json(), "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
            }, timeout=15)
            if sr.status_code != 200:
                return {"success": False, "error": f"Swap {sr.status_code}"}

            swap_tx = sr.json().get("swapTransaction")
            if not swap_tx:
                return {"success": False, "error": "No swap tx"}

            import base64 as b64
            from solders.transaction import VersionedTransaction
            tx = VersionedTransaction.from_bytes(b64.b64decode(swap_tx))
            signed = VersionedTransaction(tx.message, [keypair])
            send = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [b64.b64encode(bytes(signed)).decode(),
                           {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}],
            }, timeout=30)
            r = send.json()
            if "result" in r:
                return {"success": True, "tx_hash": r["result"]}
            err = r.get("error", {})
            return {"success": False, "error": err.get("message", str(err)) if isinstance(err, dict) else str(err)}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _execute_sell(token_mint: str, sell_pct: float = 100) -> dict:
    try:
        keypair = _get_keypair()
        wallet = str(keypair.pubkey())
        jupiter = "https://api.jup.ag/swap/v1"
        _hk = os.getenv("HELIUS_API_KEY", "")
        rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

        async with httpx.AsyncClient() as client:
            raw_amount = 0
            for prog in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                         "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
                try:
                    br = await client.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                        "params": [wallet, {"programId": prog}, {"encoding": "jsonParsed"}],
                    }, timeout=8)
                    if br.status_code == 200:
                        data = br.json()
                        for acc in data.get("result", {}).get("value", []):
                            info = acc["account"]["data"]["parsed"]["info"]
                            if info.get("mint") == token_mint:
                                raw_amount = max(raw_amount, int(info["tokenAmount"]["amount"]))
                except Exception:
                    pass
                if raw_amount > 0:
                    break

            if raw_amount <= 0:
                return {"success": False, "error": "No balance"}

            sell_amount = int(raw_amount * sell_pct / 100)
            if sell_amount <= 0:
                return {"success": False, "error": "Amount too small"}

            qr = await client.get(f"{jupiter}/quote", params={
                "inputMint": token_mint, "outputMint": SOL_MINT,
                "amount": str(sell_amount), "slippageBps": "1500",
            }, timeout=10)
            if qr.status_code != 200:
                return {"success": False, "error": f"Quote {qr.status_code}"}

            sr = await client.post(f"{jupiter}/swap", json={
                "quoteResponse": qr.json(), "userPublicKey": wallet,
                "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
            }, timeout=15)
            if sr.status_code != 200:
                return {"success": False, "error": f"Swap {sr.status_code}"}

            swap_tx = sr.json().get("swapTransaction")
            if not swap_tx:
                return {"success": False, "error": "No swap tx"}

            import base64 as b64
            from solders.transaction import VersionedTransaction
            tx = VersionedTransaction.from_bytes(b64.b64decode(swap_tx))
            signed = VersionedTransaction(tx.message, [keypair])
            send = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [b64.b64encode(bytes(signed)).decode(),
                           {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}],
            }, timeout=30)
            r = send.json()
            if "result" in r:
                return {"success": True, "tx_hash": r["result"]}
            err = r.get("error", {})
            return {"success": False, "error": err.get("message", str(err)) if isinstance(err, dict) else str(err)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# === TELEGRAM ===

async def _send_telegram(msg: str):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if token and chat_id:
            async with httpx.AsyncClient() as c:
                await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown",
                                   "disable_web_page_preview": True}, timeout=10)
    except Exception:
        pass


# === SIGNAL BUFFER ===

_signal_buffer: dict[str, list[dict]] = defaultdict(list)
_traded_tokens: dict[str, float] = {}
_daily_loss_sol: float = 0.0
_daily_loss_date: str = ""


async def _get_daily_loss() -> float:
    global _daily_loss_sol, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss_sol = 0.0
        _daily_loss_date = today
    return _daily_loss_sol


async def _track_daily_loss(loss: float):
    global _daily_loss_sol, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss_sol = 0.0
        _daily_loss_date = today
    _daily_loss_sol += loss


def _add_signal(token_addr: str, source: str, symbol: str = "?", data: dict = None):
    _signal_buffer[token_addr].append({
        "source": source,
        "ts": time.time(),
        "symbol": symbol,
        "data": data or {},
    })


def _prune_buffer(window_seconds: int = 60):
    now = time.time()
    cutoff = now - window_seconds
    empty = []
    for addr, signals in _signal_buffer.items():
        _signal_buffer[addr] = [s for s in signals if s["ts"] >= cutoff]
        if not _signal_buffer[addr]:
            empty.append(addr)
    for addr in empty:
        del _signal_buffer[addr]


# === SIGNAL COLLECTORS ===

async def _collect_cluster_signals():
    """Poll DB for smart money cluster signals from the meme bot."""
    while True:
        try:
            async with async_session() as db:
                from sqlalchemy import text
                rows = (await db.execute(text("""
                    SELECT token_address, token_symbol, wallet_count, mc_at_signal
                    FROM cluster_signals
                    WHERE detected_at > NOW() - INTERVAL '60 seconds'
                      AND wallet_count >= 2
                """))).fetchall()
                for row in rows:
                    _add_signal(row[0], "cluster_signal", row[1] or "?",
                                {"wallets": row[2], "mc": float(row[3] or 0)})
        except Exception as e:
            _log.debug(f"Cluster collector error: {e}")
        await asyncio.sleep(10)


async def _collect_dexscreener():
    """Scan DexScreener for tokens with confirmed momentum."""
    while True:
        try:
            async with httpx.AsyncClient() as client:
                # Token boosts — promoted/trending tokens
                resp = await client.get("https://api.dexscreener.com/token-boosts/latest/v1",
                                        timeout=8)
                if resp.status_code == 200:
                    boosts = resp.json()
                    if isinstance(boosts, list):
                        for b in boosts[:20]:
                            if b.get("chainId") != "solana":
                                continue
                            addr = b.get("tokenAddress", "")
                            if not addr:
                                continue
                            try:
                                pr = await client.get(
                                    f"https://api.dexscreener.com/token-pairs/v1/solana/{addr}",
                                    timeout=5)
                                if pr.status_code == 200:
                                    pairs = pr.json()
                                    if isinstance(pairs, list) and pairs:
                                        p = pairs[0]
                                        pc = p.get("priceChange", {})
                                        txns = p.get("txns", {})
                                        m5 = float(pc.get("m5", 0) or 0)
                                        m5_buys = int(txns.get("m5", {}).get("buys", 0) or 0)
                                        m5_sells = int(txns.get("m5", {}).get("sells", 0) or 0)
                                        vol_m5 = float(p.get("volume", {}).get("m5", 0) or 0)
                                        mc = float(p.get("marketCap", 0) or 0)
                                        ratio = m5_buys / max(m5_sells, 1)

                                        if m5 >= 3 and ratio >= 1.1 and vol_m5 >= 500:
                                            sym = p.get("baseToken", {}).get("symbol", "?")
                                            _add_signal(addr, "momentum", sym,
                                                        {"m5": m5, "ratio": ratio, "mc": mc, "vol": vol_m5})
                            except Exception:
                                pass
                            await asyncio.sleep(0.3)
        except Exception as e:
            _log.debug(f"DexScreener collector error: {e}")
        await asyncio.sleep(15)


async def _collect_gmgn():
    """Poll GMGN for smart money and KOL buys."""
    while True:
        try:
            from ..services.gmgn_client import get_smart_money_trades, get_kol_trades

            # Smart money trades
            sm = await get_smart_money_trades()
            if sm and isinstance(sm.get("data"), list):
                now = time.time()
                for trade in sm["data"][:50]:
                    if trade.get("side") != "buy":
                        continue
                    addr = trade.get("token_address", "")
                    if not addr:
                        continue
                    ts = trade.get("timestamp", 0)
                    if ts and (now - ts) < 120:
                        sym = trade.get("token_symbol", "?")
                        usd = float(trade.get("usd_value", 0) or 0)
                        _add_signal(addr, "gmgn_smart", sym, {"usd": usd})

            await asyncio.sleep(5)

            # KOL trades
            kol = await get_kol_trades()
            if kol and isinstance(kol.get("data"), list):
                now = time.time()
                for trade in kol["data"][:50]:
                    if trade.get("side") != "buy":
                        continue
                    addr = trade.get("token_address", "")
                    if not addr:
                        continue
                    ts = trade.get("timestamp", 0)
                    if ts and (now - ts) < 120:
                        sym = trade.get("token_symbol", "?")
                        _add_signal(addr, "gmgn_kol", sym, {"usd": float(trade.get("usd_value", 0) or 0)})

        except Exception as e:
            _log.debug(f"GMGN collector error: {e}")
        await asyncio.sleep(15)


async def _collect_graduation_events():
    """Listen for pump.fun graduation events via Redis pub/sub."""
    while True:
        try:
            from ..core.redis import redis_client
            pubsub = redis_client.pubsub()
            await pubsub.subscribe("graduation_events")
            async for msg in pubsub.listen():
                if msg.get("type") == "message":
                    try:
                        data = _json.loads(msg["data"])
                        mint = data.get("mint", "")
                        sym = data.get("symbol", "?")
                        if mint:
                            _add_signal(mint, "graduation", sym)
                    except Exception:
                        pass
        except Exception as e:
            _log.debug(f"Graduation listener error: {e}")
            await asyncio.sleep(5)


# === CONVERGENCE EVALUATOR ===

async def _evaluate_convergence():
    """Every 5 seconds: check signal buffer for convergence and enter."""
    await asyncio.sleep(10)  # Let collectors warm up

    while True:
        try:
            config = await get_config()
            if not config.get("enabled"):
                await asyncio.sleep(30)
                continue

            min_sources = config.get("min_convergence_sources", 3)
            window = config.get("convergence_window_seconds", 60)
            _prune_buffer(window)

            for addr, signals in list(_signal_buffer.items()):
                sources = set(s["source"] for s in signals)
                if len(sources) < min_sources:
                    continue

                # Convergence detected!
                symbol = signals[-1].get("symbol", "?")
                source_list = sorted(sources)
                _log.info(f"CONVERGENCE: ${symbol} — {len(sources)} sources: {', '.join(source_list)}")

                # Dedup — already traded recently?
                if addr in _traded_tokens:
                    cooldown = config.get("cooldown_hours", 4) * 3600
                    if time.time() - _traded_tokens[addr] < cooldown:
                        _log.info(f"  SKIP: ${symbol} traded in last {config.get('cooldown_hours', 4)}h")
                        continue

                # Daily loss check
                daily_loss = await _get_daily_loss()
                if daily_loss >= config["daily_loss_limit_sol"]:
                    _log.info(f"  SKIP: daily loss {daily_loss:.2f} >= limit")
                    continue

                # Max positions
                async with async_session() as db:
                    open_count = (await db.execute(
                        select(func.count()).select_from(ConvergencePosition)
                        .where(ConvergencePosition.status == "OPEN")
                    )).scalar() or 0
                    if open_count >= config["max_open_positions"]:
                        _log.info(f"  SKIP: {open_count} open >= max {config['max_open_positions']}")
                        continue

                    # Already traded this token?
                    ever = (await db.execute(
                        select(func.count()).select_from(ConvergencePosition)
                        .where(ConvergencePosition.token_address == addr)
                    )).scalar() or 0
                    if ever > 0:
                        _traded_tokens[addr] = time.time()
                        continue

                # MC + liquidity check via DexScreener
                try:
                    async with httpx.AsyncClient() as client:
                        dr = await client.get(
                            f"https://api.dexscreener.com/token-pairs/v1/solana/{addr}", timeout=5)
                        if dr.status_code != 200:
                            _log.info(f"  SKIP: ${symbol} no DexScreener data")
                            continue
                        pairs = dr.json()
                        if not isinstance(pairs, list) or not pairs:
                            _log.info(f"  SKIP: ${symbol} no pairs")
                            continue
                        pair = pairs[0]
                        mc = float(pair.get("marketCap", 0) or 0)
                        liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                        price = float(pair.get("priceUsd", 0) or 0)

                        if mc < config["min_mc_usd"] or mc > config["max_mc_usd"]:
                            _log.info(f"  SKIP: ${symbol} MC ${mc:,.0f} outside range")
                            continue
                        if liq < config.get("min_liquidity_usd", 10000):
                            _log.info(f"  SKIP: ${symbol} liquidity ${liq:,.0f} too low")
                            continue
                except Exception as e:
                    _log.info(f"  SKIP: ${symbol} DexScreener error: {e}")
                    continue

                # Security check
                try:
                    from ..services.gmgn_client import get_token_security
                    sec = await get_token_security(addr)
                    if sec:
                        sd = sec.get("data", {})
                        if sd.get("is_honeypot") == "yes":
                            _log.info(f"  SKIP: ${symbol} honeypot")
                            continue
                        if float(sd.get("sell_tax", 0) or 0) > 0.10:
                            _log.info(f"  SKIP: ${symbol} high sell tax")
                            continue
                        if float(sd.get("top_10_holder_rate", 0) or 0) > 0.40:
                            _log.info(f"  SKIP: ${symbol} top10 holders >40%")
                            continue
                except Exception:
                    pass

                # === ENTRY ===
                _traded_tokens[addr] = time.time()
                mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

                tx_hash = "paper_mode"
                if not config.get("paper_mode", True):
                    result = await _execute_buy(addr, config["position_size_sol"],
                                                config.get("slippage_bps", 300))
                    if result.get("success"):
                        tx_hash = result.get("tx_hash", "")
                        _log.info(f"LIVE BUY SUCCESS: ${symbol} tx={tx_hash[:20]}...")
                    else:
                        _log.warning(f"BUY FAILED: ${symbol} — {result.get('error')}")
                        continue

                async with async_session() as db:
                    pos = ConvergencePosition(
                        token_address=addr,
                        token_symbol=symbol[:20],
                        entry_price=Decimal(str(price)) if price > 0 else Decimal("0"),
                        entry_mc=Decimal(str(mc)),
                        position_size_sol=Decimal(str(config["position_size_sol"])),
                        signal_sources=_json.dumps(source_list),
                        signal_count=len(sources),
                        tx_hash_buy=tx_hash,
                    )
                    db.add(pos)
                    await db.flush()

                    trade = ConvergenceTrade(
                        position_id=pos.id, action="BUY",
                        price=Decimal(str(price)),
                        amount_sol=Decimal(str(config["position_size_sol"])),
                        reason=f"Convergence: {', '.join(source_list)}",
                        tx_hash=tx_hash,
                    )
                    db.add(trade)
                    await db.commit()

                _log.info(f"{'🎯' if mode == 'LIVE' else '📝'} {mode} CONVERGENCE BUY: ${symbol} "
                          f"MC=${mc:,.0f} — {len(sources)} signals: {', '.join(source_list)}")
                await _send_telegram(
                    f"{'🎯' if mode == 'LIVE' else '📝'} *{mode} CONVERGENCE: ${symbol}*\n"
                    f"Sources: {', '.join(source_list)}\n"
                    f"MC: ${mc:,.0f} | Size: {config['position_size_sol']} SOL"
                )

                # Clear this token from buffer
                _signal_buffer.pop(addr, None)

        except Exception as e:
            _log.error(f"Convergence evaluator error: {e}")
        await asyncio.sleep(config.get("scan_interval_seconds", 5) if 'config' in dir() else 5)


# === POSITION MANAGEMENT ===

async def _manage_positions():
    """Ratcheting TPs + trailing stop."""
    while True:
        try:
            config = await get_config()
            async with async_session() as db:
                positions = (await db.execute(
                    select(ConvergencePosition).where(ConvergencePosition.status == "OPEN")
                )).scalars().all()

            for pos in positions:
                try:
                    price = 0
                    try:
                        from ..services.price_cache import get_price
                        price, _ = await get_price(pos.token_address)
                    except Exception:
                        pass

                    if price <= 0:
                        continue

                    entry = float(pos.entry_price)
                    if entry <= 0:
                        continue

                    pnl_pct = ((price - entry) / entry * 100)
                    highest = max(float(pos.highest_price or price), price)
                    highest_pnl = ((highest - entry) / entry * 100) if entry > 0 else 0
                    remaining = float(pos.remaining_pct or 100)
                    tp1_done = bool(pos.tp1_done)

                    async with async_session() as db:
                        p = await db.get(ConvergencePosition, pos.id)
                        if p:
                            p.current_price = Decimal(str(price))
                            p.highest_price = Decimal(str(highest))
                            p.pnl_pct = Decimal(str(round(pnl_pct, 4)))
                            await db.commit()

                    # Hard stop
                    hard_stop = config.get("hard_stop_pct", 15)
                    if pnl_pct <= -hard_stop:
                        await _close_position(pos, remaining,
                                              f"Hard stop {pnl_pct:.1f}%", pnl_pct, config)
                        continue

                    # TP1: sell 34% at +20%
                    tp1 = config.get("tp1_pct", 20)
                    tp1_sell = config.get("tp1_sell_pct", 34)
                    if pnl_pct >= tp1 and not tp1_done:
                        await _close_position(pos, tp1_sell,
                                              f"TP1 +{pnl_pct:.0f}% (sell {tp1_sell}%)", pnl_pct, config)
                        async with async_session() as db:
                            p = await db.get(ConvergencePosition, pos.id)
                            if p:
                                p.tp1_done = True
                                await db.commit()
                        continue

                    # TP2: sell 33% at +50%
                    tp2 = config.get("tp2_pct", 50)
                    tp2_sell = config.get("tp2_sell_pct", 33)
                    if pnl_pct >= tp2 and tp1_done and remaining > 40:
                        await _close_position(pos, tp2_sell,
                                              f"TP2 +{pnl_pct:.0f}% (sell {tp2_sell}%)", pnl_pct, config)
                        continue

                    # Trailing stop: 15% from peak, only after TP1
                    trail_pct = config.get("trailing_distance_pct", 15)
                    trail_activate = config.get("trailing_activate_pct", 20)
                    if highest_pnl >= trail_activate:
                        trail_price = highest * (1 - trail_pct / 100)
                        if price <= trail_price:
                            await _close_position(pos, remaining,
                                                  f"Trail stop ({pnl_pct:+.1f}%, peak +{highest_pnl:.0f}%)",
                                                  pnl_pct, config)
                            continue

                except Exception as e:
                    _log.debug(f"Manage error {pos.token_symbol}: {e}")

        except Exception as e:
            _log.debug(f"Position manager error: {e}")
        await asyncio.sleep(5)


async def _close_position(pos: ConvergencePosition, sell_pct: float, reason: str,
                          pnl_pct: float, config: dict):
    remaining = float(pos.remaining_pct or 100) - sell_pct
    mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

    if not config.get("paper_mode", True):
        try:
            actual_sell_pct = min(sell_pct / float(pos.remaining_pct or 100) * 100, 100)
            result = await _execute_sell(pos.token_address, sell_pct=actual_sell_pct)
            if result.get("success"):
                _log.info(f"LIVE SELL SUCCESS: ${pos.token_symbol} tx={result.get('tx_hash', '')[:20]}...")
            else:
                _log.warning(f"SELL FAILED ${pos.token_symbol}: {result.get('error')}")
        except Exception as e:
            _log.warning(f"SELL ERROR ${pos.token_symbol}: {e}")

    async with async_session() as db:
        p = await db.get(ConvergencePosition, pos.id)
        if p:
            p.remaining_pct = Decimal(str(max(remaining, 0)))
            if remaining <= 0:
                p.status = "CLOSED"
                p.close_reason = reason
                p.closed_at = datetime.utcnow()

            price = float(p.current_price or p.entry_price)
            sol_value = float(p.position_size_sol) * (sell_pct / 100) * (1 + pnl_pct / 100)

            trade = ConvergenceTrade(
                position_id=pos.id, action="SELL",
                price=Decimal(str(price)),
                amount_sol=Decimal(str(round(sol_value, 6))),
                pnl_pct=Decimal(str(round(pnl_pct, 4))),
                reason=reason[:100],
            )
            db.add(trade)
            await db.commit()

        if remaining <= 0 and pnl_pct < 0:
            loss = float(pos.position_size_sol) * abs(pnl_pct) / 100
            await _track_daily_loss(loss)

    emoji = "🟢" if pnl_pct > 0 else "🔴"
    _log.info(f"{emoji} {mode} CONVERGENCE SELL: ${pos.token_symbol} {sell_pct:.0f}% @ {pnl_pct:+.1f}% — {reason}")
    await _send_telegram(
        f"{emoji} *{mode} CONVERGENCE EXIT: ${pos.token_symbol}*\n{reason}\nPnL: {pnl_pct:+.1f}%"
    )


# === MAIN ===

async def run():
    _log.info("Convergence Bot starting")
    await asyncio.sleep(3)

    config = await get_config()
    if not config.get("enabled"):
        _log.info("Convergence bot disabled")
        while True:
            await asyncio.sleep(300)

    mode = "PAPER MODE" if config.get("paper_mode", True) else "LIVE MODE"
    _log.info(f"Convergence Bot: {mode} — MULTI-SIGNAL CONVERGENCE STRATEGY")
    _log.info(f"  Min sources: {config.get('min_convergence_sources', 3)} | "
              f"Window: {config.get('convergence_window_seconds', 60)}s | "
              f"Size: {config['position_size_sol']} SOL | Max: {config['max_open_positions']}")
    _log.info(f"  Exit: TP1 +{config.get('tp1_pct', 20)}% sell {config.get('tp1_sell_pct', 34)}%, "
              f"TP2 +{config.get('tp2_pct', 50)}% sell {config.get('tp2_sell_pct', 33)}%, "
              f"trail {config.get('trailing_distance_pct', 15)}%, stop -{config.get('hard_stop_pct', 15)}%")
    _log.info(f"  MC: ${config['min_mc_usd']:,.0f}-${config['max_mc_usd']:,.0f}")

    # Load traded tokens from DB
    try:
        async with async_session() as db:
            all_addrs = (await db.execute(select(ConvergencePosition.token_address))).scalars().all()
            for a in all_addrs:
                _traded_tokens[a] = time.time()
            _log.info(f"Loaded {len(_traded_tokens)} previously traded tokens")
    except Exception:
        pass

    async def stats_loop():
        while True:
            await asyncio.sleep(60)
            total_signals = sum(len(v) for v in _signal_buffer.values())
            tokens_with_signals = len(_signal_buffer)
            multi = sum(1 for v in _signal_buffer.values() if len(set(s["source"] for s in v)) >= 2)
            _log.info(f"CONVERGENCE STATS: {total_signals} signals on {tokens_with_signals} tokens | "
                      f"{multi} with 2+ sources")

    await asyncio.gather(
        _collect_cluster_signals(),
        _collect_dexscreener(),
        _collect_gmgn(),
        _collect_graduation_events(),
        _evaluate_convergence(),
        _manage_positions(),
        stats_loop(),
    )

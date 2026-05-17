# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Momentum Breakout Bot — scans ALL Solana tokens for confirmed upward momentum.

Unlike the other 4 bots which rely on external signals (GMGN, wallet copies,
bonding curves), this bot uses pure price action: if a token is actively
rising with strong buy pressure, ride the wave. Ultra-tight exits.

Data: DexScreener API (all Solana DEX pairs)
Execution: Jupiter swap API
"""
import asyncio
import json as _json
import logging
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("momentum-bot")

MOMENTUM_WALLET_KEY_ENV = "MIGRATION_WALLET_PRIVATE_KEY"
SOL_MINT = "So11111111111111111111111111111111111111112"

DEFAULT_CONFIG = {
    "enabled": True,
    "paper_mode": True,

    "position_size_sol": 0.10,
    "max_open_positions": 3,
    "daily_loss_limit_sol": 0.30,

    # Breakout signal thresholds
    "min_m5_pct": 3.0,
    "max_m5_pct": 30.0,
    "min_h1_pct": 0.0,
    "min_buy_sell_ratio_m5": 1.1,
    "min_volume_m5_usd": 500,
    "min_mc_usd": 100000,
    "max_mc_usd": 10000000,
    "min_liquidity_usd": 10000,

    # Exit rules — ultra-tight
    "tp1_pct": 12,
    "tp1_sell_pct": 50,
    "tp2_pct": 25,
    "tp2_sell_pct": 50,
    "trailing_activate_pct": 6,
    "trailing_distance_pct": 6,
    "stop_loss_pct": 6,
    "stagnation_seconds": 20,
    "max_hold_seconds": 180,

    "slippage_bps": 300,
    "cooldown_hours": 4,
    "scan_interval_seconds": 10,

    # DexScreener search terms to scan
    "scan_searches": ["pump", "sol meme", "raydium sol", "orca sol", "jupiter sol"],
}


async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("momentum_bot_config")
        if stored:
            return {**DEFAULT_CONFIG, **_json.loads(stored)}
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


def _get_keypair():
    from solders.keypair import Keypair
    import base58 as b58
    pk = os.getenv(MOMENTUM_WALLET_KEY_ENV, "")
    if not pk:
        raise ValueError(f"{MOMENTUM_WALLET_KEY_ENV} not set")
    if pk.startswith("["):
        return Keypair.from_bytes(bytes(_json.loads(pk)))
    return Keypair.from_bytes(b58.b58decode(pk))


async def _execute_buy(token_mint: str, amount_sol: float, slippage_bps: int = 300) -> dict:
    """Buy a token via Jupiter using the momentum wallet."""
    try:
        keypair = _get_keypair()
        jupiter = "https://api.jup.ag/swap/v1"
        _hk = os.getenv("HELIUS_API_KEY", "")
        rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        amount_lamports = int(amount_sol * 1e9)

        async with httpx.AsyncClient() as client:
            qr = await client.get(f"{jupiter}/quote", params={
                "inputMint": SOL_MINT, "outputMint": token_mint,
                "amount": str(amount_lamports), "slippageBps": str(slippage_bps),
            }, timeout=10)
            if qr.status_code != 200:
                return {"success": False, "error": f"Quote failed {qr.status_code}"}

            sr = await client.post(f"{jupiter}/swap", json={
                "quoteResponse": qr.json(),
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
            }, timeout=15)
            if sr.status_code != 200:
                return {"success": False, "error": f"Swap failed {sr.status_code}"}

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
    """Sell tokens via Jupiter using the momentum wallet. Supports partial sells."""
    try:
        keypair = _get_keypair()
        wallet = str(keypair.pubkey())
        jupiter = "https://api.jup.ag/swap/v1"
        _hk = os.getenv("HELIUS_API_KEY", "")
        rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

        async with httpx.AsyncClient() as client:
            raw_amount = 0
            # Scan both token programs — mint filter broken on Helius for Token-2022
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
                return {"success": False, "error": f"Quote failed {qr.status_code}"}

            sr = await client.post(f"{jupiter}/swap", json={
                "quoteResponse": qr.json(),
                "userPublicKey": wallet,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
            }, timeout=15)
            if sr.status_code != 200:
                return {"success": False, "error": f"Swap failed {sr.status_code}"}

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


class MomentumPosition(Base):
    __tablename__ = "momentum_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dex_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    entry_price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    entry_mc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    entry_m5_pct: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    entry_h1_pct: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    entry_buy_ratio: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    position_size_sol: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    remaining_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    close_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_hash_buy: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_mom_status", "status"),
        Index("idx_mom_token", "token_address"),
    )


class MomentumTrade(Base):
    __tablename__ = "momentum_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    amount_sol: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# === STATE ===
_daily_loss = 0.0
_daily_loss_date = ""
_traded_tokens: dict[str, float] = {}  # token_addr -> last_trade_timestamp
_last_prices: dict[str, float] = {}  # token_addr -> last known price (for stagnation)


async def _send_telegram(text: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                              json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                                    "disable_web_page_preview": True}, timeout=10)
    except Exception:
        pass


async def _get_daily_loss() -> float:
    global _daily_loss, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss = 0.0
        _daily_loss_date = today
    return _daily_loss


async def _track_daily_loss(loss: float):
    global _daily_loss, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss = 0.0
        _daily_loss_date = today
    _daily_loss += loss


# === SCANNING ===

async def _scan_breakouts(config: dict) -> list[dict]:
    """Scan DexScreener for tokens with active momentum breakouts."""
    breakouts = []
    seen_addrs = set()

    async with httpx.AsyncClient() as client:
        # Also scan boosted tokens — these are actively promoted and often moving
        try:
            boost_resp = await client.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=8)
            if boost_resp.status_code == 200:
                for t in boost_resp.json():
                    if t.get("chainId") == "solana":
                        addr = t.get("tokenAddress", "")
                        if addr and addr not in seen_addrs:
                            try:
                                pr = await client.get(
                                    f"https://api.dexscreener.com/token-pairs/v1/solana/{addr}", timeout=5)
                                if pr.status_code == 200:
                                    bpairs = pr.json() if isinstance(pr.json(), list) else pr.json().get("pairs", [])
                                    if bpairs:
                                        # Add to the pairs we'll evaluate below
                                        seen_addrs.add(addr)
                                        p = bpairs[0]
                                        p["chainId"] = "solana"
                                        p["baseToken"] = p.get("baseToken", {"address": addr})
                                        # Evaluate inline
                                        pc = p.get("priceChange", {})
                                        txns = p.get("txns", {})
                                        vol = p.get("volume", {})
                                        m5_pct = float(pc.get("m5", 0) or 0)
                                        h1_pct = float(pc.get("h1", 0) or 0)
                                        mc = float(p.get("fdv", 0) or 0)
                                        liq = float(p.get("liquidity", {}).get("usd", 0) or 0) if isinstance(p.get("liquidity"), dict) else 0
                                        vol_m5 = float(vol.get("m5", 0) or 0) if isinstance(vol, dict) else 0
                                        buys_m5 = int(txns.get("m5", {}).get("buys", 0) or 0)
                                        sells_m5 = int(txns.get("m5", {}).get("sells", 0) or 0)
                                        buy_ratio = buys_m5 / max(sells_m5, 1)
                                        if (m5_pct >= config["min_m5_pct"] and m5_pct <= config.get("max_m5_pct", 25)
                                                and h1_pct >= config["min_h1_pct"] and buy_ratio >= config["min_buy_sell_ratio_m5"]
                                                and mc >= config["min_mc_usd"] and mc <= config["max_mc_usd"]
                                                and liq >= config["min_liquidity_usd"]):
                                            breakouts.append({
                                                "address": addr, "symbol": p.get("baseToken", {}).get("symbol", "?")[:20],
                                                "dex": p.get("dexId", "?"), "price": float(p.get("priceUsd", 0) or 0),
                                                "mc": mc, "liquidity": liq, "m5_pct": m5_pct, "h1_pct": h1_pct,
                                                "buy_ratio": buy_ratio, "buys_m5": buys_m5, "sells_m5": sells_m5, "vol_m5": vol_m5,
                                            })
                            except Exception:
                                pass
        except Exception:
            pass

        # Scan token profiles (recently active tokens)
        try:
            prof_resp = await client.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=8)
            if prof_resp.status_code == 200:
                for t in prof_resp.json():
                    if t.get("chainId") == "solana":
                        addr = t.get("tokenAddress", "")
                        if addr and addr not in seen_addrs:
                            try:
                                pr = await client.get(
                                    f"https://api.dexscreener.com/token-pairs/v1/solana/{addr}", timeout=3)
                                if pr.status_code == 200:
                                    bpairs = pr.json() if isinstance(pr.json(), list) else pr.json().get("pairs", [])
                                    if bpairs:
                                        seen_addrs.add(addr)
                                        p = bpairs[0]
                                        pc = p.get("priceChange", {})
                                        txns = p.get("txns", {})
                                        vol = p.get("volume", {})
                                        m5 = float(pc.get("m5", 0) or 0)
                                        h1 = float(pc.get("h1", 0) or 0)
                                        mc = float(p.get("fdv", 0) or 0)
                                        liq = float(p.get("liquidity", {}).get("usd", 0) or 0) if isinstance(p.get("liquidity"), dict) else 0
                                        vol_m5 = float(vol.get("m5", 0) or 0) if isinstance(vol, dict) else 0
                                        buys = int(txns.get("m5", {}).get("buys", 0) or 0)
                                        sells = int(txns.get("m5", {}).get("sells", 0) or 0)
                                        ratio = buys / max(sells, 1)
                                        max_m5 = config.get("max_m5_pct", 30)
                                        if (m5 >= config["min_m5_pct"] and m5 <= max_m5
                                                and h1 >= config["min_h1_pct"] and ratio >= config["min_buy_sell_ratio_m5"]
                                                and mc >= config["min_mc_usd"] and mc <= config["max_mc_usd"]
                                                and liq >= config["min_liquidity_usd"]):
                                            breakouts.append({
                                                "address": addr, "symbol": p.get("baseToken", {}).get("symbol", "?")[:20],
                                                "dex": p.get("dexId", "?"), "price": float(p.get("priceUsd", 0) or 0),
                                                "mc": mc, "liquidity": liq, "m5_pct": m5, "h1_pct": h1,
                                                "buy_ratio": ratio, "buys_m5": buys, "sells_m5": sells, "vol_m5": vol_m5,
                                            })
                            except Exception:
                                pass
        except Exception:
            pass

        # Scan latest Solana pairs (new listings)
        try:
            new_resp = await client.get("https://api.dexscreener.com/latest/dex/pairs/solana", timeout=8)
            if new_resp.status_code == 200:
                for p in new_resp.json().get("pairs", []):
                    addr = p.get("baseToken", {}).get("address", "")
                    if addr and addr not in seen_addrs:
                        seen_addrs.add(addr)
                        pc = p.get("priceChange", {})
                        txns = p.get("txns", {})
                        vol = p.get("volume", {})
                        m5 = float(pc.get("m5", 0) or 0)
                        h1 = float(pc.get("h1", 0) or 0)
                        mc = float(p.get("fdv", 0) or 0)
                        liq = float(p.get("liquidity", {}).get("usd", 0) or 0) if isinstance(p.get("liquidity"), dict) else 0
                        vol_m5 = float(vol.get("m5", 0) or 0) if isinstance(vol, dict) else 0
                        buys = int(txns.get("m5", {}).get("buys", 0) or 0)
                        sells = int(txns.get("m5", {}).get("sells", 0) or 0)
                        ratio = buys / max(sells, 1)
                        max_m5 = config.get("max_m5_pct", 30)
                        if (m5 >= config["min_m5_pct"] and m5 <= max_m5
                                and h1 >= config["min_h1_pct"] and ratio >= config["min_buy_sell_ratio_m5"]
                                and mc >= config["min_mc_usd"] and mc <= config["max_mc_usd"]
                                and liq >= config["min_liquidity_usd"]):
                            breakouts.append({
                                "address": addr, "symbol": p.get("baseToken", {}).get("symbol", "?")[:20],
                                "dex": p.get("dexId", "?"), "price": float(p.get("priceUsd", 0) or 0),
                                "mc": mc, "liquidity": liq, "m5_pct": m5, "h1_pct": h1,
                                "buy_ratio": ratio, "buys_m5": buys, "sells_m5": sells, "vol_m5": vol_m5,
                            })
        except Exception:
            pass

        # Scan pump.fun currently live tokens
        try:
            pf_resp = await client.get("https://frontend-api-v3.pump.fun/coins/currently-live",
                params={"limit": 50, "offset": 0, "includeNsfw": "false"},
                headers={"User-Agent": "AgiotageMomentum/1.0"}, timeout=8)
            if pf_resp.status_code == 200:
                for t in pf_resp.json():
                    if isinstance(t, dict) and not t.get("complete"):
                        mint = t.get("mint", "")
                        mc = float(t.get("usd_market_cap", 0) or 0)
                        if mint and mint not in seen_addrs and mc >= config["min_mc_usd"]:
                            seen_addrs.add(mint)
                            try:
                                pr = await client.get(
                                    f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}", timeout=3)
                                if pr.status_code == 200:
                                    bpairs = pr.json() if isinstance(pr.json(), list) else pr.json().get("pairs", [])
                                    if bpairs:
                                        p = bpairs[0]
                                        pc = p.get("priceChange", {})
                                        m5 = float(pc.get("m5", 0) or 0)
                                        h1 = float(pc.get("h1", 0) or 0)
                                        txns = p.get("txns", {})
                                        buys = int(txns.get("m5", {}).get("buys", 0) or 0)
                                        sells = int(txns.get("m5", {}).get("sells", 0) or 0)
                                        ratio = buys / max(sells, 1)
                                        liq = float(p.get("liquidity", {}).get("usd", 0) or 0) if isinstance(p.get("liquidity"), dict) else 0
                                        vol_m5 = float(p.get("volume", {}).get("m5", 0) or 0) if isinstance(p.get("volume"), dict) else 0
                                        max_m5 = config.get("max_m5_pct", 30)
                                        if (m5 >= config["min_m5_pct"] and m5 <= max_m5 and h1 >= config["min_h1_pct"]
                                                and ratio >= config["min_buy_sell_ratio_m5"]
                                                and mc <= config["max_mc_usd"] and liq >= config.get("min_liquidity_usd", 0)):
                                            breakouts.append({
                                                "address": mint, "symbol": t.get("symbol", "?")[:20],
                                                "dex": "pumpfun", "price": float(p.get("priceUsd", 0) or 0),
                                                "mc": mc, "liquidity": liq, "m5_pct": m5, "h1_pct": h1,
                                                "buy_ratio": ratio, "buys_m5": buys, "sells_m5": sells, "vol_m5": vol_m5,
                                            })
                            except Exception:
                                pass
        except Exception:
            pass

        # Original DexScreener search
        for search_term in config.get("scan_searches", ["pump", "sol meme"]):
            try:
                resp = await client.get(
                    "https://api.dexscreener.com/latest/dex/search",
                    params={"q": search_term}, timeout=8)
                if resp.status_code != 200:
                    continue

                pairs = resp.json().get("pairs", [])
                for p in pairs:
                    if p.get("chainId") != "solana":
                        continue

                    addr = p.get("baseToken", {}).get("address", "")
                    if not addr or addr in seen_addrs:
                        continue
                    seen_addrs.add(addr)

                    pc = p.get("priceChange", {})
                    txns = p.get("txns", {})
                    vol = p.get("volume", {})

                    m5_pct = float(pc.get("m5", 0) or 0)
                    h1_pct = float(pc.get("h1", 0) or 0)
                    mc = float(p.get("fdv", 0) or 0)
                    liq = float(p.get("liquidity", {}).get("usd", 0) or 0) if isinstance(p.get("liquidity"), dict) else 0
                    vol_m5 = float(vol.get("m5", 0) or 0) if isinstance(vol, dict) else 0
                    buys_m5 = int(txns.get("m5", {}).get("buys", 0) or 0)
                    sells_m5 = int(txns.get("m5", {}).get("sells", 0) or 0)
                    buy_ratio = buys_m5 / max(sells_m5, 1)

                    # Volume acceleration: m5 volume should be disproportionately high
                    vol_h1 = float(vol.get("h1", 0) or 0) if isinstance(vol, dict) else 0
                    vol_accelerating = vol_m5 > (vol_h1 / 12) * 1.5 if vol_h1 > 0 else vol_m5 > 0

                    # Spike filter: >25% in 5min is a spike, not sustained momentum
                    max_m5 = config.get("max_m5_pct", 25)

                    if (m5_pct >= config["min_m5_pct"]
                            and m5_pct <= max_m5
                            and h1_pct >= config["min_h1_pct"]
                            and buy_ratio >= config["min_buy_sell_ratio_m5"]
                            and vol_m5 >= config["min_volume_m5_usd"]
                            and mc >= config["min_mc_usd"]
                            and mc <= config["max_mc_usd"]
                            and liq >= config["min_liquidity_usd"]):
                        breakouts.append({
                            "address": addr,
                            "symbol": p.get("baseToken", {}).get("symbol", "???")[:20],
                            "dex": p.get("dexId", "?"),
                            "price": float(p.get("priceUsd", 0) or 0),
                            "mc": mc,
                            "liquidity": liq,
                            "m5_pct": m5_pct,
                            "h1_pct": h1_pct,
                            "buy_ratio": buy_ratio,
                            "buys_m5": buys_m5,
                            "sells_m5": sells_m5,
                            "vol_m5": vol_m5,
                        })

            except Exception as e:
                _log.debug(f"Scan error for '{search_term}': {e}")

    # Sort by m5 momentum (strongest first)
    breakouts.sort(key=lambda x: x["m5_pct"], reverse=True)
    return breakouts


# === ENTRY ===

async def _evaluate_and_enter(breakout: dict, config: dict) -> bool:
    """Evaluate a breakout signal and enter if it passes all filters."""
    addr = breakout["address"]
    symbol = breakout["symbol"]

    # Cooldown check
    last_trade = _traded_tokens.get(addr, 0)
    cooldown = config.get("cooldown_hours", 4) * 3600
    if time.time() - last_trade < cooldown:
        return False

    # Daily loss check
    daily = await _get_daily_loss()
    if daily >= config["daily_loss_limit_sol"]:
        return False

    # Max positions
    async with async_session() as db:
        open_count = (await db.execute(
            select(func.count()).select_from(MomentumPosition)
            .where(MomentumPosition.status == "OPEN")
        )).scalar() or 0
        if open_count >= config["max_open_positions"]:
            return False

        ever_traded = (await db.execute(
            select(func.count()).select_from(MomentumPosition)
            .where(MomentumPosition.token_address == addr,
                   MomentumPosition.opened_at >= datetime.utcnow() - timedelta(hours=config.get("cooldown_hours", 4)))
        )).scalar() or 0
        if ever_traded > 0:
            return False

    # Security check
    try:
        from ..services.gmgn_client import get_token_security
        sec = await get_token_security(addr)
        if sec:
            data = sec.get("data", {})
            if data.get("is_honeypot") == "yes":
                _log.info(f"SKIP ${symbol}: honeypot")
                return False
            if float(data.get("sell_tax", 0) or 0) > 0.10:
                _log.info(f"SKIP ${symbol}: high sell tax")
                return False
            top10 = float(data.get("top_10_holder_rate", 0) or 0)
            if top10 > 0.40:
                _log.info(f"SKIP ${symbol}: top10={top10:.0%}")
                return False
    except Exception:
        pass

    # ENTRY
    _traded_tokens[addr] = time.time()
    price = breakout["price"]
    mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

    _log.info(f"🚀 BREAKOUT: ${symbol} 5m={breakout['m5_pct']:+.0f}% 1h={breakout['h1_pct']:+.0f}% "
              f"MC=${breakout['mc']:,.0f} buys/sells={breakout['buys_m5']}/{breakout['sells_m5']} "
              f"liq=${breakout['liquidity']:,.0f}")

    tx_hash = "paper_mode"
    if not config.get("paper_mode", True):
        try:
            result = await _execute_buy(addr, config["position_size_sol"],
                                        slippage_bps=config.get("slippage_bps", 300))
            if result and result.get("success"):
                tx_hash = result.get("tx_hash", "")
                _log.info(f"LIVE BUY SUCCESS: ${symbol} tx={tx_hash[:20]}...")
            else:
                _log.warning(f"BUY FAILED: ${symbol} — {result.get('error', '?')}")
                return False
        except Exception as e:
            _log.warning(f"BUY FAILED: ${symbol} — {e}")
            return False

    async with async_session() as db:
        pos = MomentumPosition(
            token_address=addr,
            token_symbol=symbol,
            dex_id=breakout["dex"],
            entry_price=Decimal(str(price)),
            entry_mc=Decimal(str(breakout["mc"])),
            entry_m5_pct=Decimal(str(breakout["m5_pct"])),
            entry_h1_pct=Decimal(str(breakout["h1_pct"])),
            entry_buy_ratio=Decimal(str(round(breakout["buy_ratio"], 2))),
            position_size_sol=Decimal(str(config["position_size_sol"])),
            current_price=Decimal(str(price)),
            highest_price=Decimal(str(price)),
            tx_hash_buy=tx_hash,
        )
        db.add(pos)
        await db.flush()

        trade = MomentumTrade(
            position_id=pos.id, action="BUY",
            price=Decimal(str(price)),
            amount_sol=Decimal(str(config["position_size_sol"])),
            reason=f"Breakout 5m={breakout['m5_pct']:+.0f}% b/s={breakout['buys_m5']}/{breakout['sells_m5']}",
            tx_hash=tx_hash,
        )
        db.add(trade)
        await db.commit()

    _log.info(f"{mode} MOMENTUM BUY: ${symbol} @ ${price:.6f} ({config['position_size_sol']} SOL)")
    await _send_telegram(
        f"🚀 *{mode} MOMENTUM: ${symbol}*\n"
        f"5m: {breakout['m5_pct']:+.0f}% | 1h: {breakout['h1_pct']:+.0f}%\n"
        f"MC: ${breakout['mc']:,.0f} | Liq: ${breakout['liquidity']:,.0f}\n"
        f"Buys/Sells: {breakout['buys_m5']}/{breakout['sells_m5']}\n"
        f"[Chart](https://dexscreener.com/solana/{addr})"
    )
    return True


# === POSITION MANAGEMENT ===

async def _manage_positions(config: dict):
    """Ultra-tight exit engine for momentum trades."""
    async with async_session() as db:
        positions = (await db.execute(
            select(MomentumPosition).where(MomentumPosition.status == "OPEN")
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
                age = (datetime.utcnow() - pos.opened_at).total_seconds()
                if age >= config["max_hold_seconds"]:
                    await _close_position(pos, float(pos.remaining_pct),
                                          "Timeout (no price)", 0, config)
                continue

            entry = float(pos.entry_price)
            pnl_pct = ((price - entry) / entry * 100) if entry > 0 else 0
            highest = max(float(pos.highest_price or price), price)
            remaining = float(pos.remaining_pct)
            age = (datetime.utcnow() - pos.opened_at).total_seconds()

            # Update DB
            async with async_session() as db:
                p = await db.get(MomentumPosition, pos.id)
                if p:
                    p.current_price = Decimal(str(price))
                    p.highest_price = Decimal(str(highest))
                    p.pnl_pct = Decimal(str(round(pnl_pct, 4)))
                    await db.commit()

            # === STOP LOSS ===
            if pnl_pct <= -config["stop_loss_pct"]:
                await _close_position(pos, remaining,
                                      f"Stop loss {pnl_pct:.1f}%", pnl_pct, config)
                continue

            # === TP1 ===
            if pnl_pct >= config["tp1_pct"] and remaining > config["tp1_sell_pct"]:
                await _close_position(pos, config["tp1_sell_pct"],
                                      f"TP1 +{pnl_pct:.0f}%", pnl_pct, config)
                continue

            # === TP2 ===
            if pnl_pct >= config["tp2_pct"] and remaining > 0:
                await _close_position(pos, remaining,
                                      f"TP2 +{pnl_pct:.0f}%", pnl_pct, config)
                continue

            # === TRAILING STOP ===
            trail_act = config.get("trailing_activate_pct", 8)
            trail_dist = config.get("trailing_distance_pct", 8)
            if highest > 0 and entry > 0:
                highest_pnl = ((highest - entry) / entry * 100)
                if highest_pnl >= trail_act:
                    trail_stop = highest * (1 - trail_dist / 100)
                    if price <= trail_stop:
                        await _close_position(pos, remaining,
                                              f"Trail ({pnl_pct:+.1f}%, peak {highest_pnl:+.0f}%)",
                                              pnl_pct, config)
                        continue

            # === STAGNATION ===
            last_price = _last_prices.get(pos.token_address, 0)
            if last_price > 0 and abs(price - last_price) / last_price < 0.005:
                # Price hasn't moved >0.5% — check for stagnation duration
                stag_key = f"stag_{pos.token_address}"
                if stag_key not in _last_prices:
                    _last_prices[stag_key] = time.time()
                elif time.time() - _last_prices[stag_key] >= config["stagnation_seconds"]:
                    await _close_position(pos, remaining,
                                          f"Stagnation ({pnl_pct:+.1f}%)", pnl_pct, config)
                    del _last_prices[stag_key]
                    continue
            else:
                stag_key = f"stag_{pos.token_address}"
                _last_prices.pop(stag_key, None)
            _last_prices[pos.token_address] = price

            # === TIMEOUT ===
            if age >= config["max_hold_seconds"]:
                await _close_position(pos, remaining,
                                      f"Timeout {age:.0f}s ({pnl_pct:+.1f}%)", pnl_pct, config)
                continue

        except Exception as e:
            _log.debug(f"Manage error {pos.token_symbol}: {e}")


async def _close_position(pos: MomentumPosition, sell_pct: float, reason: str,
                          pnl_pct: float, config: dict):
    """Close part or all of a momentum position."""
    remaining = float(pos.remaining_pct) - sell_pct
    mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

    if not config.get("paper_mode", True):
        try:
            actual_sell_pct = min(sell_pct / float(pos.remaining_pct) * 100, 100) if float(pos.remaining_pct) > 0 else 100
            result = await _execute_sell(pos.token_address, sell_pct=actual_sell_pct)
            if result.get("success"):
                _log.info(f"LIVE SELL SUCCESS: ${pos.token_symbol} {sell_pct:.0f}% tx={result.get('tx_hash','')[:20]}...")
            else:
                _log.warning(f"SELL FAILED ${pos.token_symbol}: {result.get('error', '?')}")
        except Exception as e:
            _log.warning(f"SELL FAILED ${pos.token_symbol}: {e}")

    async with async_session() as db:
        p = await db.get(MomentumPosition, pos.id)
        if p:
            p.remaining_pct = Decimal(str(max(remaining, 0)))
            if remaining <= 0:
                p.status = "CLOSED"
                p.close_reason = reason
                p.closed_at = datetime.utcnow()

            price = float(p.current_price or p.entry_price)
            sol_value = float(p.position_size_sol) * (sell_pct / 100) * (1 + pnl_pct / 100)

            trade = MomentumTrade(
                position_id=pos.id, action="SELL",
                price=Decimal(str(price)),
                amount_sol=Decimal(str(round(sol_value, 6))),
                pnl_pct=Decimal(str(round(pnl_pct, 4))),
                reason=reason[:100],
            )
            db.add(trade)
            await db.commit()

        if pnl_pct < 0 and remaining <= 0:
            loss = float(pos.position_size_sol) * abs(pnl_pct) / 100
            await _track_daily_loss(loss)

    emoji = "🟢" if pnl_pct > 0 else "🔴"
    _log.info(f"{mode} MOMENTUM SELL: ${pos.token_symbol} {sell_pct:.0f}% @ {pnl_pct:+.1f}% — {reason}")
    await _send_telegram(
        f"{emoji} *{mode} MOMENTUM SELL: ${pos.token_symbol}*\n"
        f"{reason}\nPnL: {pnl_pct:+.1f}%"
    )


# === MAIN LOOP ===

async def run():
    _log.info("Momentum Breakout Bot starting")
    await asyncio.sleep(5)

    config = await get_config()
    mode = "PAPER MODE" if config.get("paper_mode", True) else "LIVE MODE"
    _log.info(f"Momentum Bot: {mode} — size={config['position_size_sol']} SOL, "
              f"max={config['max_open_positions']}, MC=${config['min_mc_usd']:,}-${config['max_mc_usd']:,}, "
              f"5m>{config['min_m5_pct']}%, stop={config['stop_loss_pct']}%, "
              f"TP1={config['tp1_pct']}%/TP2={config['tp2_pct']}%, max_hold={config['max_hold_seconds']}s")

    if not config.get("enabled"):
        _log.info("Momentum bot disabled")
        while True:
            await asyncio.sleep(300)

    # Two loops: fast position management (5s) + slower entry scanning (10s)
    _scan_count = 0

    async def scan_loop():
        nonlocal _scan_count
        while True:
            try:
                cfg = await get_config()
                breakouts = await _scan_breakouts(cfg)
                _scan_count += 1
                if breakouts:
                    _log.info(f"Found {len(breakouts)} breakouts")
                    for b in breakouts[:3]:
                        _log.info(f"  ${b['symbol']} 5m={b['m5_pct']:+.0f}% h1={b['h1_pct']:+.0f}% "
                                  f"MC=${b['mc']:,.0f} b/s={b['buys_m5']}/{b['sells_m5']}")
                        entered = await _evaluate_and_enter(b, cfg)
                        if entered:
                            break
                elif _scan_count % 60 == 0:
                    _log.info(f"Scan #{_scan_count}: no breakouts found (scanning every {cfg.get('scan_interval_seconds', 10)}s)")
            except Exception as e:
                _log.error(f"Scan error: {e}")
            await asyncio.sleep(cfg.get("scan_interval_seconds", 10))

    async def manage_loop():
        while True:
            try:
                cfg = await get_config()
                await _manage_positions(cfg)
            except Exception as e:
                _log.debug(f"Manage error: {e}")
            await asyncio.sleep(5)

    await asyncio.gather(scan_loop(), manage_loop())

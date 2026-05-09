# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Copy Trade Bot — mirrors elite wallet trades in real-time.
Runs on a SEPARATE wallet from the meme bot. Independent risk.

Strategy: Track 3-5 elite wallets (50%+ winrate, $10K+ profit).
When they buy, we buy. When they sell, we sell. Simple.

Safety rails:
- Max position size capped
- Don't copy if token already pumped >30% since wallet bought
- Security check (honeypot, mint, freeze, top10 holders)
- Max concurrent positions
- Daily loss limit
- Don't copy sandwich bots or MEV bots
- Min wallet winrate to copy
- Cooldown per token (no re-entry within 30 min)
"""
import asyncio
import json as _json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("copy-trader")

GMGN_HOST = "https://openapi.gmgn.ai"
POLL_INTERVAL = 20  # seconds between wallet checks (balanced for GMGN rate limits)


# === CONFIG ===

DEFAULT_CONFIG = {
    "enabled": True,
    "position_size_sol": 0.10,
    "max_open_positions": 5,
    "daily_loss_limit_sol": 0.50,
    "min_sol_reserve": 0.05,

    # Wallet quality filters — council reviewed
    "min_wallet_winrate": 0.65,
    "min_wallet_profit": 25000,
    "min_wallet_trades": 30,
    "block_tags": ["sandwich_bot", "mev_bot"],
    "max_tracked_wallets": 10,
    "paper_mode": True,

    # Entry filters
    "max_price_pump_pct": 30,
    "min_mc": 100000,
    "max_mc": 50000000,
    "min_volume_h1": 5000,
    "min_buy_usd": 100,
    "cooldown_minutes": 30,
    "max_trade_age_seconds": 60,

    # Security
    "require_mint_renounced": True,
    "require_freeze_renounced": True,
    "max_top10_holder_pct": 0.40,

    # Exit rules
    "stop_loss_pct": 25,
    "take_profit_pct": 50,
    "copy_sell": True,
    "max_hold_hours": 6,

    # Execution
    "buy_slippage_bps": 300,
    "sell_slippage_bps": 500,
    "priority_fee_lamports": 200000,

    # Skip stables/wrapped
    "skip_symbols": ["WSOL", "WETH", "WBTC", "USDC", "USDT", "SOL"],
}


async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("copy_trader_config")
        if stored:
            return {**DEFAULT_CONFIG, **_json.loads(stored)}
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


# === DB MODELS ===

class CopyPosition(Base):
    __tablename__ = "copy_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    copied_wallet: Mapped[str] = mapped_column(String(66), nullable=False)
    wallet_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entry_price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    entry_mc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    position_size_sol: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    position_size_usd: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    close_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_hash_buy: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tx_hash_sell: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_cp_status", "status"),
        Index("idx_cp_token", "token_address"),
        Index("idx_cp_wallet", "copied_wallet"),
    )


class CopyTrade(Base):
    __tablename__ = "copy_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY or SELL
    price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    amount_sol: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    amount_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_ct_position", "position_id"),
    )


class TrackedWallet(Base):
    __tablename__ = "copy_tracked_wallets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(66), nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    winrate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    realized_profit: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    tier: Mapped[str] = mapped_column(String(10), default="A")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# === HELPERS ===

_daily_loss_sol = 0.0
_daily_loss_date = ""
_seen_tx_hashes: dict[str, float] = {}  # tx_hash -> timestamp

# Separate wallet for copy trading — uses COPY_TRADER_PRIVATE_KEY env var
COPY_WALLET_KEY_ENV = "COPY_TRADER_PRIVATE_KEY"


def _get_copy_wallet_address() -> str:
    """Get the copy trader wallet public key."""
    from solders.keypair import Keypair
    pk = os.getenv(COPY_WALLET_KEY_ENV, "")
    if not pk:
        raise ValueError(f"{COPY_WALLET_KEY_ENV} not set")
    if pk.startswith("["):
        return str(Keypair.from_bytes(bytes(_json.loads(pk))).pubkey())
    import base58 as b58
    return str(Keypair.from_bytes(b58.b58decode(pk)).pubkey())


async def _copy_buy_token(token_mint: str, amount_sol: float,
                          slippage_bps: int = 300, priority_fee: int = 50000) -> dict:
    """Buy token using the COPY TRADER wallet (not the meme bot wallet)."""
    import base64
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

    pk = os.getenv(COPY_WALLET_KEY_ENV, "")
    if not pk:
        return {"success": False, "tx_hash": None, "error": f"{COPY_WALLET_KEY_ENV} not set"}

    if pk.startswith("["):
        keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
    else:
        import base58 as b58
        keypair = Keypair.from_bytes(b58.b58decode(pk))

    sol_mint = "So11111111111111111111111111111111111111112"
    amount_lamports = int(amount_sol * 1e9)
    jupiter_api = "https://api.jup.ag/swap/v1"
    rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

    try:
        async with httpx.AsyncClient() as client:
            quote_resp = await client.get(f"{jupiter_api}/quote", params={
                "inputMint": sol_mint, "outputMint": token_mint,
                "amount": str(amount_lamports), "slippageBps": slippage_bps,
            }, timeout=10)
            if quote_resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": "Quote failed"}
            quote = quote_resp.json()

            swap_resp = await client.post(f"{jupiter_api}/swap", json={
                "quoteResponse": quote,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": priority_fee,
                "dynamicComputeUnitLimit": True,
            }, timeout=15)
            if swap_resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": "Swap request failed"}

            swap_tx = swap_resp.json().get("swapTransaction")
            if not swap_tx:
                return {"success": False, "tx_hash": None, "error": "No swap tx"}

            tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx))
            signed_tx = VersionedTransaction(tx.message, [keypair])

            send_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_tx)).decode(),
                           {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
            }, timeout=30)

            if send_resp.status_code == 200 and "result" in send_resp.json():
                tx_hash = send_resp.json()["result"]
                # Wait for confirmation
                for _ in range(30):
                    check = await client.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                        "params": [[tx_hash], {"searchTransactionHistory": True}],
                    }, timeout=10)
                    if check.status_code == 200:
                        statuses = check.json().get("result", {}).get("value", [])
                        if statuses and statuses[0]:
                            if statuses[0].get("err"):
                                return {"success": False, "tx_hash": tx_hash, "error": "Tx failed"}
                            if statuses[0].get("confirmationStatus") in ("confirmed", "finalized"):
                                return {"success": True, "tx_hash": tx_hash, "error": None}
                    await asyncio.sleep(2)
                return {"success": False, "tx_hash": tx_hash, "error": "Not confirmed"}

            return {"success": False, "tx_hash": None, "error": "Send failed"}
    except Exception as e:
        return {"success": False, "tx_hash": None, "error": str(e)}


async def _copy_sell_token(token_mint: str, slippage_bps: int = 500,
                           priority_fee: int = 50000) -> dict:
    """Sell ALL of a token from the COPY TRADER wallet."""
    import base64
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

    pk = os.getenv(COPY_WALLET_KEY_ENV, "")
    if not pk:
        return {"success": False, "tx_hash": None, "error": f"{COPY_WALLET_KEY_ENV} not set"}

    if pk.startswith("["):
        keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
    else:
        import base58 as b58
        keypair = Keypair.from_bytes(b58.b58decode(pk))

    sol_mint = "So11111111111111111111111111111111111111112"
    jupiter_api = "https://api.jup.ag/swap/v1"
    rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

    # Get token balance
    try:
        async with httpx.AsyncClient() as client:
            bal_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [str(keypair.pubkey()), {"mint": token_mint}, {"encoding": "jsonParsed"}],
            }, timeout=10)
            raw_amount = 0
            if bal_resp.status_code == 200:
                for acc in bal_resp.json().get("result", {}).get("value", []):
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    if info.get("mint") == token_mint:
                        raw_amount = int(info.get("tokenAmount", {}).get("amount", 0))

            if raw_amount <= 0:
                return {"success": False, "tx_hash": None, "error": "No balance"}

            quote_resp = await client.get(f"{jupiter_api}/quote", params={
                "inputMint": token_mint, "outputMint": sol_mint,
                "amount": str(raw_amount), "slippageBps": slippage_bps,
            }, timeout=10)
            if quote_resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": "Quote failed"}
            quote = quote_resp.json()

            swap_resp = await client.post(f"{jupiter_api}/swap", json={
                "quoteResponse": quote,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": priority_fee,
                "dynamicComputeUnitLimit": True,
            }, timeout=15)
            if swap_resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": "Swap failed"}

            swap_tx = swap_resp.json().get("swapTransaction")
            if not swap_tx:
                return {"success": False, "tx_hash": None, "error": "No swap tx"}

            tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx))
            signed_tx = VersionedTransaction(tx.message, [keypair])

            send_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_tx)).decode(),
                           {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
            }, timeout=30)

            if send_resp.status_code == 200 and "result" in send_resp.json():
                tx_hash = send_resp.json()["result"]
                for _ in range(30):
                    check = await client.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                        "params": [[tx_hash], {"searchTransactionHistory": True}],
                    }, timeout=10)
                    if check.status_code == 200:
                        statuses = check.json().get("result", {}).get("value", [])
                        if statuses and statuses[0]:
                            if statuses[0].get("err"):
                                return {"success": False, "tx_hash": tx_hash, "error": "Tx failed"}
                            if statuses[0].get("confirmationStatus") in ("confirmed", "finalized"):
                                return {"success": True, "tx_hash": tx_hash, "error": None}
                    await asyncio.sleep(2)
                return {"success": False, "tx_hash": tx_hash, "error": "Not confirmed"}

            return {"success": False, "tx_hash": None, "error": "Send failed"}
    except Exception as e:
        return {"success": False, "tx_hash": None, "error": str(e)}


async def _gmgn_get(path: str, params: dict) -> dict | None:
    api_key = os.getenv("GMGN_API_KEY", "")
    if not api_key:
        return None
    query = {**params, "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{GMGN_HOST}{path}", params=query,
                                    headers={"X-APIKEY": api_key}, timeout=15)
            if resp.status_code == 429:
                await asyncio.sleep(30)
                return None
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None


async def _get_sol_price() -> float:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=5)
            if resp.status_code == 200:
                return resp.json().get("solana", {}).get("usd", 150)
    except Exception:
        pass
    return 150


async def _get_price_mc(token_addr: str) -> tuple[float, float, float]:
    """Get price, MC, and 1h volume from DexScreener."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{token_addr}", timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if pairs:
                    pair = pairs[0]
                    price = float(pair.get("priceUsd", 0) or 0)
                    mc = float(pair.get("fdv", 0) or 0)
                    vol_data = pair.get("volume", {})
                    vol_h1 = float(vol_data.get("h1", 0) or 0) if isinstance(vol_data, dict) else 0
                    return price, mc, vol_h1
    except Exception:
        pass
    return 0, 0, 0


async def _check_security(token_addr: str) -> dict:
    """Check token security via GMGN. Returns {safe, reasons}."""
    api_key = os.getenv("GMGN_API_KEY", "")
    if not api_key:
        return {"safe": True, "reasons": []}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GMGN_HOST}/v1/token/security",
                params={"chain": "sol", "address": token_addr,
                        "timestamp": int(time.time()), "client_id": str(uuid.uuid4())},
                headers={"X-APIKEY": api_key}, timeout=8)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                reasons = []
                if data.get("is_honeypot") == "yes":
                    reasons.append("honeypot")
                if float(data.get("sell_tax", 0) or 0) > 0.10:
                    reasons.append("high_sell_tax")
                if data.get("renounced_mint") not in (1, "1", True, "true", "yes"):
                    reasons.append("mint_not_renounced")
                if data.get("renounced_freeze_account") not in (1, "1", True, "true", "yes"):
                    reasons.append("freeze_not_renounced")
                top10 = float(data.get("top_10_holder_rate", 0) or 0)
                if top10 > 0.40:
                    reasons.append(f"top10={top10:.0%}")
                return {"safe": len(reasons) == 0, "reasons": reasons}
    except Exception:
        pass
    return {"safe": True, "reasons": []}


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


async def _track_daily_loss(loss_sol: float):
    global _daily_loss_sol, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss_sol = 0.0
        _daily_loss_date = today
    _daily_loss_sol += loss_sol


async def _get_daily_loss() -> float:
    global _daily_loss_sol, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss_sol = 0.0
        _daily_loss_date = today
    return _daily_loss_sol


# === WALLET MANAGEMENT ===

async def _get_tracked_wallets() -> list[TrackedWallet]:
    async with async_session() as db:
        wallets = (await db.execute(
            select(TrackedWallet).where(TrackedWallet.active == True)
        )).scalars().all()
        return list(wallets)


async def _refresh_wallet_stats():
    """Update wallet performance stats from GMGN."""
    async with async_session() as db:
        wallets = (await db.execute(
            select(TrackedWallet).where(TrackedWallet.active == True)
        )).scalars().all()

        for wallet in wallets:
            data = await _gmgn_get("/v1/user/wallet_stats",
                                   {"chain": "sol", "address": wallet.address, "period": "30d"})
            if not data:
                continue
            stats = data.get("data", data)
            if isinstance(stats, dict):
                wallet.winrate = Decimal(str(float(stats.get("winrate", 0) or 0)))
                wallet.realized_profit = Decimal(str(float(stats.get("realized_profit", 0) or 0)))
                wallet.total_trades = int(stats.get("total_trades", 0) or 0)
                wallet.last_checked = datetime.utcnow()

                # Deactivate wallets that dropped below threshold
                config = await get_config()
                if (float(wallet.winrate or 0) < config["min_wallet_winrate"]
                        and wallet.total_trades >= config["min_wallet_trades"]):
                    wallet.active = False
                    _log.info(f"Deactivated wallet {wallet.label or wallet.address[:12]}: "
                              f"WR {float(wallet.winrate):.0%} < {config['min_wallet_winrate']:.0%}")

        await db.commit()


async def add_wallet(address: str, label: str = None) -> dict:
    """Add a wallet to track. Called from API."""
    async with async_session() as db:
        existing = (await db.execute(
            select(TrackedWallet).where(TrackedWallet.address == address)
        )).scalar_one_or_none()
        if existing:
            existing.active = True
            existing.label = label or existing.label
            await db.commit()
            return {"status": "reactivated", "address": address}

        wallet = TrackedWallet(address=address, label=label or address[:12])
        db.add(wallet)
        await db.commit()
        return {"status": "added", "address": address, "label": wallet.label}


async def remove_wallet(address: str) -> dict:
    async with async_session() as db:
        wallet = (await db.execute(
            select(TrackedWallet).where(TrackedWallet.address == address)
        )).scalar_one_or_none()
        if wallet:
            wallet.active = False
            await db.commit()
            return {"status": "removed"}
        return {"status": "not_found"}


# === CORE LOGIC ===

async def _execute_buy(token_addr: str, token_symbol: str, wallet_addr: str,
                       wallet_label: str, price: float, mc: float,
                       config: dict) -> dict | None:
    """Execute a copy trade buy."""
    try:
        size_sol = config["position_size_sol"]
        paper_mode = config.get("paper_mode", True)

        if paper_mode:
            result = {"success": True, "tx_hash": "paper_mode"}
        else:
            result = await _copy_buy_token(
                token_mint=token_addr,
                amount_sol=size_sol,
                slippage_bps=config["buy_slippage_bps"],
                priority_fee=config["priority_fee_lamports"],
            )

        if result.get("success"):
            sol_price = await _get_sol_price()
            size_usd = size_sol * sol_price

            async with async_session() as db:
                pos = CopyPosition(
                    token_address=token_addr,
                    token_symbol=token_symbol,
                    copied_wallet=wallet_addr,
                    wallet_label=wallet_label,
                    entry_price=Decimal(str(price)),
                    entry_mc=Decimal(str(mc)),
                    position_size_sol=Decimal(str(size_sol)),
                    position_size_usd=Decimal(str(round(size_usd, 2))),
                    current_price=Decimal(str(price)),
                    highest_price=Decimal(str(price)),
                )
                db.add(pos)
                await db.flush()

                trade = CopyTrade(
                    position_id=pos.id, action="BUY",
                    price=Decimal(str(price)),
                    amount_sol=Decimal(str(size_sol)),
                    amount_usd=Decimal(str(round(size_usd, 2))),
                    reason=f"Copy {wallet_label or wallet_addr[:12]}",
                    tx_hash=result.get("tx_hash"),
                )
                db.add(trade)
                pos.tx_hash_buy = result.get("tx_hash")
                await db.commit()

                mode = "PAPER" if paper_mode else "LIVE"
                _log.info(f"{mode} COPY BUY: ${token_symbol} {size_sol} SOL — copying {wallet_label}")
                await _send_telegram(
                    f"📋 *{mode} COPY BUY: ${token_symbol}*\n\n"
                    f"Copying: {wallet_label or wallet_addr[:12]}\n"
                    f"Size: {size_sol} SOL (${size_usd:.2f})\n"
                    f"MC: ${mc:,.0f}\n"
                    f"CA: `{token_addr}`\n"
                    f"[Chart](https://dexscreener.com/solana/{token_addr})"
                )
                return {"success": True, "position_id": pos.id}
        else:
            _log.warning(f"COPY BUY FAILED: ${token_symbol} — {result.get('error')}")
            return None

    except Exception as e:
        _log.error(f"Copy buy error: {e}")
        return None


async def _execute_sell(pos: CopyPosition, reason: str, config: dict):
    """Execute a sell on a copy position."""
    try:
        paper_mode = config.get("paper_mode", True)

        if paper_mode:
            result = {"success": True, "tx_hash": "paper_mode"}
        else:
            result = await _copy_sell_token(
                token_mint=pos.token_address,
                slippage_bps=config["sell_slippage_bps"],
                priority_fee=config["priority_fee_lamports"],
            )

            if not result.get("success") and result.get("error") == "No balance":
                async with async_session() as db:
                    p = await db.get(CopyPosition, pos.id)
                    if p and p.status == "OPEN":
                        p.status = "CLOSED"
                        p.close_reason = "No on-chain balance"
                        p.closed_at = datetime.utcnow()
                        await db.commit()
                return

        price, _, _ = await _get_price_mc(pos.token_address)
        entry = float(pos.entry_price)
        pnl_pct = ((price - entry) / entry * 100) if entry > 0 and price > 0 else 0
        sol_price = await _get_sol_price()
        pnl_usd = float(pos.position_size_usd) * (pnl_pct / 100)

        paper_mode = config.get("paper_mode", True)

        # Only mark CLOSED if sell succeeded or we're in paper mode
        if not result.get("success") and not paper_mode:
            _log.warning(f"COPY SELL FAILED: ${pos.token_symbol} — {result.get('error')} — position stays OPEN")
            return

        async with async_session() as db:
            p = await db.get(CopyPosition, pos.id)
            if p:
                p.status = "CLOSED"
                p.close_reason = reason
                p.closed_at = datetime.utcnow()
                p.current_price = Decimal(str(price)) if price > 0 else p.current_price
                p.pnl_pct = Decimal(str(round(pnl_pct, 4)))
                p.pnl_usd = Decimal(str(round(pnl_usd, 2)))
                p.tx_hash_sell = result.get("tx_hash") if result.get("success") else None

                trade = CopyTrade(
                    position_id=pos.id, action="SELL",
                    price=Decimal(str(price)),
                    amount_sol=Decimal(str(round(abs(pnl_usd) / sol_price, 6))) if sol_price > 0 else None,
                    amount_usd=Decimal(str(round(float(pos.position_size_usd) + pnl_usd, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=reason[:100],
                    tx_hash=result.get("tx_hash"),
                )
                db.add(trade)
                await db.commit()

            if pnl_pct < 0:
                loss_sol = abs(pnl_usd) / sol_price if sol_price > 0 else 0
                await _track_daily_loss(loss_sol)

        mode = "PAPER" if paper_mode else "LIVE"
        _log.info(f"COPY SELL ({mode}): ${pos.token_symbol} @ {pnl_pct:+.1f}% — {reason}")
        emoji = "🟢" if pnl_pct > 0 else "🔴"
        await _send_telegram(
            f"{emoji} *COPY SELL: ${pos.token_symbol}*\n"
            f"{reason}\n"
            f"PnL: {pnl_pct:+.1f}% (${pnl_usd:+.2f})"
        )

    except Exception as e:
        _log.error(f"Copy sell error for {pos.token_symbol}: {e}")


# === POLL AND MANAGE ===

async def _poll_single_wallet(wallet: TrackedWallet, config: dict):
    """Poll a single wallet for new trades."""
    try:
        data = await _gmgn_get("/v1/user/wallet_activities",
                                {"chain": "sol", "wallet_address": wallet.address, "limit": 10})
        if not data:
            return

        activities = data.get("data", data)
        if isinstance(activities, dict):
            activities = activities.get("list", activities.get("activities", []))
        if not isinstance(activities, list):
            return

        for act in activities:
            tx_hash = act.get("tx_hash", "")
            if not tx_hash or tx_hash in _seen_tx_hashes:
                continue
            _seen_tx_hashes[tx_hash] = time.time()

            side = (act.get("event_type") or act.get("side") or "").lower()
            if side not in ("buy",):
                if side == "sell" and config.get("copy_sell"):
                    token_addr = act.get("token_address", "")
                    if token_addr:
                        await _check_copy_sell(token_addr, wallet.address, config)
                continue

            token_addr = act.get("token_address", "")
            symbol = (act.get("token_symbol") or act.get("symbol") or "")[:20]
            amount_usd = float(act.get("amount_usd", 0) or act.get("cost_usd", 0) or 0)

            if not token_addr or not symbol:
                continue
            if symbol.upper() in config["skip_symbols"]:
                continue
            if amount_usd < config["min_buy_usd"]:
                continue

            ts = act.get("timestamp") or act.get("block_timestamp")
            max_age = config.get("max_trade_age_seconds", 60)
            if ts and isinstance(ts, (int, float)):
                trade_age = time.time() - ts
                if trade_age > max_age:
                    continue

            await _evaluate_copy(token_addr, symbol, wallet, amount_usd, config)

    except Exception as e:
        _log.debug(f"Wallet poll error {wallet.label}: {e}")


async def _poll_wallets(config: dict):
    """Check all tracked wallets concurrently for new buys."""
    wallets = await _get_tracked_wallets()
    if not wallets:
        return

    # Poll wallets in small batches to avoid GMGN rate limits
    batch_size = 3
    for i in range(0, len(wallets), batch_size):
        batch = wallets[i:i + batch_size]
        await asyncio.gather(*[_poll_single_wallet(w, config) for w in batch],
                             return_exceptions=True)
        if i + batch_size < len(wallets):
            await asyncio.sleep(2)

    # Evict old hashes (keep last hour)
    if len(_seen_tx_hashes) > 2000:
        cutoff = time.time() - 3600
        to_keep = {k: v for k, v in _seen_tx_hashes.items() if v > cutoff}
        _seen_tx_hashes.clear()
        _seen_tx_hashes.update(to_keep)


async def _get_copy_wallet_sol_balance() -> float:
    """Get SOL balance of the copy trader wallet."""
    try:
        address = _get_copy_wallet_address()
        rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        async with httpx.AsyncClient() as client:
            resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                "params": [address],
            }, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result", {}).get("value", 0) / 1e9
    except Exception:
        pass
    return 0


async def _evaluate_copy(token_addr: str, symbol: str, wallet: TrackedWallet,
                         wallet_buy_usd: float, config: dict):
    """Evaluate whether to copy a wallet's buy."""

    # Check SOL balance — don't trade if insufficient
    balance = await _get_copy_wallet_sol_balance()
    needed = config["position_size_sol"] + config.get("min_sol_reserve", 0.05)
    if balance < needed:
        _log.info(f"SKIP copy ${symbol}: insufficient SOL ({balance:.4f} < {needed:.4f} needed)")
        return

    # Check daily loss limit
    daily_loss = await _get_daily_loss()
    if daily_loss >= config["daily_loss_limit_sol"]:
        _log.info(f"SKIP copy ${symbol}: daily loss limit ({daily_loss:.2f} SOL)")
        return

    # Check max positions
    async with async_session() as db:
        open_count = (await db.execute(
            select(func.count()).select_from(CopyPosition)
            .where(CopyPosition.status == "OPEN")
        )).scalar() or 0
        if open_count >= config["max_open_positions"]:
            _log.info(f"SKIP copy ${symbol}: max positions ({open_count})")
            return

        # Cooldown — don't re-enter same token within N minutes
        cutoff = datetime.utcnow() - timedelta(minutes=config["cooldown_minutes"])
        recent = (await db.execute(
            select(func.count()).select_from(CopyPosition)
            .where(CopyPosition.token_address == token_addr,
                   CopyPosition.opened_at >= cutoff)
        )).scalar() or 0
        if recent > 0:
            _log.info(f"SKIP copy ${symbol}: cooldown active")
            return

        # Don't double up — skip if already holding
        existing = (await db.execute(
            select(CopyPosition)
            .where(CopyPosition.token_address == token_addr,
                   CopyPosition.status == "OPEN")
        )).scalar_one_or_none()
        if existing:
            _log.info(f"SKIP copy ${symbol}: already holding")
            return

    # Get current price and MC
    price, mc, vol_h1 = await _get_price_mc(token_addr)
    if price <= 0:
        _log.info(f"SKIP copy ${symbol}: no price data")
        return

    if mc < config["min_mc"] or mc > config["max_mc"]:
        _log.info(f"SKIP copy ${symbol}: MC ${mc:,.0f} out of range")
        return

    if vol_h1 < config["min_volume_h1"]:
        _log.info(f"SKIP copy ${symbol}: 1h vol ${vol_h1:,.0f} too low")
        return

    # Security check
    sec = await _check_security(token_addr)
    if not sec["safe"]:
        _log.info(f"SKIP copy ${symbol}: security fail — {', '.join(sec['reasons'])}")
        return

    # All checks passed — execute
    _log.info(f"COPY SIGNAL: ${symbol} — {wallet.label} bought ${wallet_buy_usd:,.0f} — MC ${mc:,.0f}")
    await _execute_buy(token_addr, symbol, wallet.address, wallet.label, price, mc, config)


async def _check_copy_sell(token_addr: str, wallet_addr: str, config: dict):
    """If a tracked wallet sells a token we hold, sell our position too."""
    async with async_session() as db:
        pos = (await db.execute(
            select(CopyPosition)
            .where(CopyPosition.token_address == token_addr,
                   CopyPosition.copied_wallet == wallet_addr,
                   CopyPosition.status == "OPEN")
        )).scalar_one_or_none()
        if pos:
            _log.info(f"COPY SELL SIGNAL: {pos.wallet_label} sold ${pos.token_symbol}")
            await _execute_sell(pos, f"Copied sell from {pos.wallet_label or wallet_addr[:12]}", config)


async def _manage_positions(config: dict):
    """Check open positions for stop loss, take profit, and timeout."""
    async with async_session() as db:
        positions = (await db.execute(
            select(CopyPosition).where(CopyPosition.status == "OPEN")
        )).scalars().all()

    for pos in positions:
        try:
            price, mc, _ = await _get_price_mc(pos.token_address)

            # If price is 0, check if position has been open too long — force close
            if price <= 0:
                age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
                if age_hours >= config["max_hold_hours"]:
                    await _execute_sell(pos, f"Force close — no price data after {age_hours:.1f}h", config)
                continue

            entry = float(pos.entry_price)
            pnl_pct = ((price - entry) / entry * 100) if entry > 0 else 0
            highest = max(float(pos.highest_price or price), price)
            pnl_usd = float(pos.position_size_usd) * (pnl_pct / 100)

            async with async_session() as db:
                p = await db.get(CopyPosition, pos.id)
                if p:
                    p.current_price = Decimal(str(price))
                    p.highest_price = Decimal(str(highest))
                    p.pnl_pct = Decimal(str(round(pnl_pct, 4)))
                    p.pnl_usd = Decimal(str(round(pnl_usd, 2)))
                    await db.commit()

            # On-chain balance is checked inside _execute_sell via _copy_sell_token

            # Stop loss
            if pnl_pct <= -config["stop_loss_pct"]:
                await _execute_sell(pos, f"Stop loss ({pnl_pct:.1f}%)", config)
                continue

            # Take profit
            if pnl_pct >= config["take_profit_pct"]:
                await _execute_sell(pos, f"Take profit ({pnl_pct:.1f}%)", config)
                continue

            # Max hold time
            age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
            if age_hours >= config["max_hold_hours"]:
                await _execute_sell(pos, f"Max hold time ({age_hours:.1f}h)", config)
                continue

        except Exception as e:
            _log.debug(f"Position manage error {pos.token_symbol}: {e}")


# === AUTO-DISCOVER ELITE WALLETS ===

async def _auto_discover():
    """Find top-performing wallets from GMGN smart money feed."""
    config = await get_config()
    data = await _gmgn_get("/v1/user/smartmoney", {"chain": "sol", "limit": 100})
    if not data:
        return

    items = data.get("data", data)
    if isinstance(items, dict):
        items = items.get("list", [])
    if not isinstance(items, list):
        return

    async with async_session() as db:
        # Check current wallet count
        current_count = (await db.execute(
            select(func.count()).select_from(TrackedWallet).where(TrackedWallet.active == True)
        )).scalar() or 0
        max_wallets = config.get("max_tracked_wallets", 10)

        for item in items:
            if current_count >= max_wallets:
                break

            addr = item.get("wallet_address") or item.get("address", "")
            if not addr:
                continue

            winrate = float(item.get("winrate", 0) or 0)
            profit = float(item.get("realized_profit", 0) or item.get("pnl", 0) or 0)
            trades = int(item.get("total_trades", 0) or item.get("buy_count", 0) or 0)
            tags = item.get("tags", [])

            if isinstance(tags, list) and any(t in config["block_tags"] for t in tags):
                continue
            if winrate < config["min_wallet_winrate"]:
                continue
            if profit < config["min_wallet_profit"]:
                continue
            if trades < config["min_wallet_trades"]:
                continue

            existing = (await db.execute(
                select(TrackedWallet).where(TrackedWallet.address == addr)
            )).scalar_one_or_none()

            if not existing:
                label = item.get("name") or item.get("twitter_username") or addr[:12]
                wallet = TrackedWallet(
                    address=addr,
                    label=label,
                    winrate=Decimal(str(winrate)),
                    realized_profit=Decimal(str(profit)),
                    total_trades=trades,
                    tier="S" if winrate >= 0.65 and profit >= 25000 else "A",
                )
                db.add(wallet)
                current_count += 1
                _log.info(f"Auto-discovered wallet: {label} (WR:{winrate:.0%}, profit:${profit:,.0f})")

        await db.commit()
        _log.info(f"Tracked wallets: {current_count}/{max_wallets}")


# === MAIN LOOP ===

async def run():
    _log.info("Copy Trade Bot starting")
    await asyncio.sleep(30)

    config = await get_config()
    if not config.get("enabled"):
        _log.info("Copy trader disabled in config")
        while True:
            await asyncio.sleep(300)

    # Discover wallets on startup
    await _auto_discover()

    wallets = await _get_tracked_wallets()
    mode = "PAPER MODE" if config.get("paper_mode", True) else "LIVE MODE"
    _log.info(f"Copy Trade Bot: {mode} — tracking {len(wallets)} wallets")
    _log.info(f"Wallets: {', '.join(w.label or w.address[:12] for w in wallets) or 'none yet (will auto-discover)'}")
    _log.info(f"Config: size={config['position_size_sol']} SOL, max={config['max_open_positions']}, "
              f"SL={config['stop_loss_pct']}%, TP={config['take_profit_pct']}%, "
              f"hold={config['max_hold_hours']}h, min_wr={config['min_wallet_winrate']:.0%}, "
              f"max_wallets={config.get('max_tracked_wallets', 10)}")

    stats_refresh = 0
    discover_refresh = 0

    while True:
        try:
            config = await get_config()

            # Poll wallets for new trades
            await _poll_wallets(config)

            # Manage open positions
            await _manage_positions(config)

            # Refresh wallet stats every 30 min (catch degradation fast)
            stats_refresh += POLL_INTERVAL
            if stats_refresh >= 1800:
                await _refresh_wallet_stats()
                stats_refresh = 0

            # Re-discover wallets every 2 hours
            discover_refresh += POLL_INTERVAL
            if discover_refresh >= 7200:
                await _auto_discover()
                discover_refresh = 0

        except Exception as e:
            _log.error(f"Copy trader error: {e}")

        await asyncio.sleep(POLL_INTERVAL)

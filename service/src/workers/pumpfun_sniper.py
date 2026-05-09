# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Pump.fun Graduation Sniper — enters tokens at 25-35% bonding curve progress.

NOT a raw launch sniper (that's negative EV). This bot waits for momentum
confirmation on the bonding curve, applies strict filters, and targets the
~15-25% of filtered tokens that graduate to PumpSwap.

Data: PumpPortal WebSocket (wss://pumpportal.fun/api/data)
Execution: pumpportal.fun/api/trade-local (returns unsigned tx, we sign locally)
"""
import asyncio
import base64
import json as _json
import logging
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("pumpfun-sniper")

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
PUMPPORTAL_API = "https://pumpportal.fun/api"
GRADUATION_SOL = 85  # ~$69K MC, tokens graduate when curve holds ~85 SOL

SNIPER_WALLET_KEY_ENV = "SNIPER_WALLET_PRIVATE_KEY"


# === CONFIG ===

DEFAULT_CONFIG = {
    "enabled": True,
    "paper_mode": True,

    # Position sizing
    "position_size_sol": 0.05,
    "max_open_positions": 5,
    "daily_loss_limit_sol": 0.50,
    "min_sol_reserve": 0.05,

    # Entry filters — bonding curve progress
    "min_curve_pct": 25,
    "max_curve_pct": 50,
    "min_holders": 50,
    "min_volume_sol": 5.0,
    "max_dev_pct": 5.0,
    "min_buy_sell_ratio": 1.5,
    "max_token_age_minutes": 120,

    # Exit rules
    "tp1_pct": 50,
    "tp1_sell_pct": 50,
    "tp2_pct": 100,
    "tp2_sell_pct": 30,
    "graduation_sell_pct": 20,
    "stop_loss_pct": 30,
    "max_hold_minutes": 120,

    # Execution
    "slippage_pct": 15,
    "priority_fee_sol": 0.0005,
}


async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("pumpfun_sniper_config")
        if stored:
            return {**DEFAULT_CONFIG, **_json.loads(stored)}
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


# === DB MODELS ===

class SnipePosition(Base):
    __tablename__ = "snipe_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mint: Mapped[str] = mapped_column(String(66), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dev_wallet: Mapped[str | None] = mapped_column(String(66), nullable=True)
    entry_price_sol: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    entry_curve_pct: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    entry_holders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_size_sol: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    tokens_held: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    remaining_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    current_price_sol: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    highest_price_sol: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    graduated: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    close_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_hash_buy: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_snipe_status", "status"),
        Index("idx_snipe_mint", "mint"),
    )


class SnipeTrade(Base):
    __tablename__ = "snipe_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    price_sol: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    amount_sol: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# === STATE ===

_daily_loss_sol = 0.0
_daily_loss_date = ""
_tracked_tokens: dict[str, dict] = {}  # mint -> token state
_seen_mints: set[str] = set()  # never re-enter a token


async def _track_daily_loss(loss: float):
    global _daily_loss_sol, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss_sol = 0.0
        _daily_loss_date = today
    _daily_loss_sol += loss


async def _get_daily_loss() -> float:
    global _daily_loss_sol, _daily_loss_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _daily_loss_date != today:
        _daily_loss_sol = 0.0
        _daily_loss_date = today
    return _daily_loss_sol


# === HELPERS ===

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


def _calc_price_from_curve(v_sol: float, v_tokens: float) -> float:
    """Calculate token price in SOL from bonding curve reserves."""
    if v_tokens <= 0:
        return 0
    return v_sol / v_tokens


def _calc_curve_pct(v_sol: float) -> float:
    """Calculate bonding curve completion percentage."""
    return (v_sol / GRADUATION_SOL) * 100


# === EXECUTION ===

async def _execute_buy(mint: str, amount_sol: float, config: dict) -> dict:
    """Buy on pump.fun bonding curve via pumpportal."""
    paper_mode = config.get("paper_mode", True)

    if paper_mode:
        return {"success": True, "tx_hash": "paper_mode"}

    pk = os.getenv(SNIPER_WALLET_KEY_ENV, "")
    if not pk:
        return {"success": False, "tx_hash": None, "error": f"{SNIPER_WALLET_KEY_ENV} not set"}

    try:
        if pk.startswith("["):
            from solders.keypair import Keypair
            keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
        else:
            from solders.keypair import Keypair
            import base58 as b58
            keypair = Keypair.from_bytes(b58.b58decode(pk))

        async with httpx.AsyncClient() as client:
            # Get unsigned transaction from pumpportal
            resp = await client.post(f"{PUMPPORTAL_API}/trade-local", json={
                "publicKey": str(keypair.pubkey()),
                "action": "buy",
                "mint": mint,
                "amount": amount_sol,
                "denominatedInSol": "true",
                "slippage": config.get("slippage_pct", 15),
                "priorityFee": config.get("priority_fee_sol", 0.0005),
                "pool": "pump",
            }, timeout=15)

            if resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": f"Pumpportal error: {resp.status_code}"}

            # Sign and send the transaction
            from solders.transaction import VersionedTransaction
            tx_bytes = resp.content
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [keypair])

            rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
            send_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_tx)).decode(),
                           {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
            }, timeout=30)

            if send_resp.status_code == 200 and "result" in send_resp.json():
                tx_hash = send_resp.json()["result"]
                return {"success": True, "tx_hash": tx_hash, "error": None}

            return {"success": False, "tx_hash": None, "error": "Send failed"}
    except Exception as e:
        return {"success": False, "tx_hash": None, "error": str(e)}


async def _execute_sell(mint: str, token_amount: float, config: dict) -> dict:
    """Sell on pump.fun bonding curve via pumpportal."""
    paper_mode = config.get("paper_mode", True)

    if paper_mode:
        return {"success": True, "tx_hash": "paper_mode"}

    pk = os.getenv(SNIPER_WALLET_KEY_ENV, "")
    if not pk:
        return {"success": False, "tx_hash": None, "error": f"{SNIPER_WALLET_KEY_ENV} not set"}

    try:
        if pk.startswith("["):
            from solders.keypair import Keypair
            keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
        else:
            from solders.keypair import Keypair
            import base58 as b58
            keypair = Keypair.from_bytes(b58.b58decode(pk))

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{PUMPPORTAL_API}/trade-local", json={
                "publicKey": str(keypair.pubkey()),
                "action": "sell",
                "mint": mint,
                "amount": token_amount,
                "denominatedInSol": "false",
                "slippage": config.get("slippage_pct", 15),
                "priorityFee": config.get("priority_fee_sol", 0.0005),
                "pool": "pump",
            }, timeout=15)

            if resp.status_code != 200:
                return {"success": False, "tx_hash": None, "error": f"Pumpportal error: {resp.status_code}"}

            from solders.transaction import VersionedTransaction
            tx_bytes = resp.content
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [keypair])

            rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
            send_resp = await client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(bytes(signed_tx)).decode(),
                           {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
            }, timeout=30)

            if send_resp.status_code == 200 and "result" in send_resp.json():
                tx_hash = send_resp.json()["result"]
                return {"success": True, "tx_hash": tx_hash, "error": None}

            return {"success": False, "tx_hash": None, "error": "Send failed"}
    except Exception as e:
        return {"success": False, "tx_hash": None, "error": str(e)}


# === SIGNAL EVALUATION ===

async def _evaluate_token(token: dict, config: dict):
    """Evaluate a token for sniping based on bonding curve progress and filters."""
    mint = token.get("mint", "")
    symbol = token.get("symbol", "")
    v_sol = token.get("vSolInBondingCurve", 0) or token.get("v_sol", 0)
    v_tokens = token.get("vTokensInBondingCurve", 0) or token.get("v_tokens", 0)

    if not mint or mint in _seen_mints:
        return

    curve_pct = _calc_curve_pct(v_sol)

    # Only enter at the sweet spot
    if curve_pct < config["min_curve_pct"] or curve_pct > config["max_curve_pct"]:
        return

    # Check daily loss
    daily_loss = await _get_daily_loss()
    if daily_loss >= config["daily_loss_limit_sol"]:
        return

    # Check wallet balance
    if not config.get("paper_mode", True):
        try:
            pk = os.getenv(SNIPER_WALLET_KEY_ENV, "")
            if pk:
                if pk.startswith("["):
                    from solders.keypair import Keypair
                    pubkey = str(Keypair.from_bytes(bytes(_json.loads(pk))).pubkey())
                else:
                    from solders.keypair import Keypair
                    import base58 as b58
                    pubkey = str(Keypair.from_bytes(b58.b58decode(pk)).pubkey())
                rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
                async with httpx.AsyncClient() as client:
                    resp = await client.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey],
                    }, timeout=10)
                    if resp.status_code == 200:
                        bal = resp.json().get("result", {}).get("value", 0) / 1e9
                        needed = config["position_size_sol"] + config.get("min_sol_reserve", 0.05)
                        if bal < needed:
                            _log.info(f"SKIP {symbol}: insufficient SOL ({bal:.4f} < {needed})")
                            return
        except Exception:
            pass

    # Check max positions
    async with async_session() as db:
        open_count = (await db.execute(
            select(func.count()).select_from(SnipePosition)
            .where(SnipePosition.status == "OPEN")
        )).scalar() or 0
        if open_count >= config["max_open_positions"]:
            return

        # 1-per-token rule
        ever_traded = (await db.execute(
            select(func.count()).select_from(SnipePosition)
            .where(SnipePosition.mint == mint)
        )).scalar() or 0
        if ever_traded > 0:
            return

    # Check holder count and volume from tracked state
    holders = token.get("holders", 0)
    volume_sol = token.get("volume_sol", 0)
    buy_count = token.get("buys", 0)
    sell_count = token.get("sells", 0)
    dev_wallet = token.get("traderPublicKey", "") or token.get("dev", "")
    dev_pct = token.get("dev_pct", 0)

    if holders < config["min_holders"]:
        _log.debug(f"SKIP {symbol}: {holders} holders < {config['min_holders']}")
        return

    if volume_sol < config["min_volume_sol"]:
        _log.debug(f"SKIP {symbol}: {volume_sol:.1f} SOL vol < {config['min_volume_sol']}")
        return

    if dev_pct > config["max_dev_pct"]:
        _log.info(f"SKIP {symbol}: dev holds {dev_pct:.1f}% > {config['max_dev_pct']}%")
        return

    if sell_count > 0 and buy_count / max(sell_count, 1) < config["min_buy_sell_ratio"]:
        _log.debug(f"SKIP {symbol}: buy/sell ratio {buy_count}/{sell_count} too low")
        return

    # Token age check
    created_at = token.get("created_at")
    if created_at:
        age_min = (time.time() - created_at) / 60
        if age_min > config["max_token_age_minutes"]:
            _log.debug(f"SKIP {symbol}: {age_min:.0f}min old > {config['max_token_age_minutes']}min")
            return

    # All filters passed — execute snipe
    _seen_mints.add(mint)
    price_sol = _calc_price_from_curve(v_sol, v_tokens)

    _log.info(f"SNIPE SIGNAL: ${symbol} curve={curve_pct:.0f}% holders={holders} vol={volume_sol:.1f}SOL")

    result = await _execute_buy(mint, config["position_size_sol"], config)
    mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

    if result.get("success"):
        tokens_received = (config["position_size_sol"] / price_sol) if price_sol > 0 else 0

        async with async_session() as db:
            pos = SnipePosition(
                mint=mint,
                symbol=symbol[:20] if symbol else None,
                name=token.get("name", "")[:100] if token.get("name") else None,
                dev_wallet=dev_wallet[:66] if dev_wallet else None,
                entry_price_sol=Decimal(str(price_sol)),
                entry_curve_pct=Decimal(str(round(curve_pct, 2))),
                entry_holders=holders,
                position_size_sol=Decimal(str(config["position_size_sol"])),
                tokens_held=Decimal(str(round(tokens_received, 2))),
                current_price_sol=Decimal(str(price_sol)),
                highest_price_sol=Decimal(str(price_sol)),
                tx_hash_buy=result.get("tx_hash"),
            )
            db.add(pos)
            await db.flush()

            trade = SnipeTrade(
                position_id=pos.id, action="BUY",
                price_sol=Decimal(str(price_sol)),
                amount_sol=Decimal(str(config["position_size_sol"])),
                reason=f"Snipe at {curve_pct:.0f}% curve, {holders} holders",
                tx_hash=result.get("tx_hash"),
            )
            db.add(trade)
            await db.commit()

        _log.info(f"{mode} SNIPE BUY: ${symbol} @ {curve_pct:.0f}% curve, {config['position_size_sol']} SOL")
        await _send_telegram(
            f"🎯 *{mode} SNIPE: ${symbol}*\n\n"
            f"Curve: {curve_pct:.0f}%\n"
            f"Holders: {holders}\n"
            f"Volume: {volume_sol:.1f} SOL\n"
            f"Size: {config['position_size_sol']} SOL\n"
            f"[pump.fun](https://pump.fun/{mint})"
        )

        # Subscribe to this token's trades for price tracking
        return mint  # caller uses this to subscribe
    else:
        _log.warning(f"SNIPE BUY FAILED: ${symbol} — {result.get('error')}")
        return None


# === POSITION MANAGEMENT ===

async def _manage_positions(config: dict):
    """Check open snipe positions for TP/SL/timeout."""
    async with async_session() as db:
        positions = (await db.execute(
            select(SnipePosition).where(SnipePosition.status == "OPEN")
        )).scalars().all()

    for pos in positions:
        try:
            token_state = _tracked_tokens.get(pos.mint, {})
            v_sol = token_state.get("v_sol", 0)
            v_tokens = token_state.get("v_tokens", 0)
            price_sol = _calc_price_from_curve(v_sol, v_tokens) if v_sol > 0 else 0

            if price_sol <= 0:
                # Check timeout even without price
                age_min = (datetime.utcnow() - pos.opened_at).total_seconds() / 60
                if age_min >= config["max_hold_minutes"]:
                    await _close_position(pos, 100, "Timeout (no price data)", 0, config)
                continue

            entry = float(pos.entry_price_sol)
            pnl_pct = ((price_sol - entry) / entry * 100) if entry > 0 else 0
            highest = max(float(pos.highest_price_sol or price_sol), price_sol)
            remaining = float(pos.remaining_pct)

            # Update DB
            async with async_session() as db:
                p = await db.get(SnipePosition, pos.id)
                if p:
                    p.current_price_sol = Decimal(str(price_sol))
                    p.highest_price_sol = Decimal(str(highest))
                    p.pnl_pct = Decimal(str(round(pnl_pct, 4)))
                    await db.commit()

            # Check graduation
            curve_pct = _calc_curve_pct(v_sol)
            if curve_pct >= 99 and remaining > 0:
                sell_pct = config["graduation_sell_pct"]
                await _close_position(pos, min(sell_pct, remaining),
                                      f"Graduation ({pnl_pct:+.1f}%)", pnl_pct, config)
                async with async_session() as db:
                    p = await db.get(SnipePosition, pos.id)
                    if p:
                        p.graduated = True
                        await db.commit()
                continue

            # TP1
            if pnl_pct >= config["tp1_pct"] and remaining > config["tp1_sell_pct"]:
                await _close_position(pos, config["tp1_sell_pct"],
                                      f"TP1 +{pnl_pct:.0f}%", pnl_pct, config)
                continue

            # TP2
            if pnl_pct >= config["tp2_pct"] and remaining > config["graduation_sell_pct"]:
                sell = min(config["tp2_sell_pct"], remaining - config["graduation_sell_pct"])
                if sell > 0:
                    await _close_position(pos, sell,
                                          f"TP2 +{pnl_pct:.0f}%", pnl_pct, config)
                continue

            # Stop loss
            if pnl_pct <= -config["stop_loss_pct"]:
                await _close_position(pos, remaining,
                                      f"Stop loss {pnl_pct:.1f}%", pnl_pct, config)
                continue

            # Timeout
            age_min = (datetime.utcnow() - pos.opened_at).total_seconds() / 60
            if age_min >= config["max_hold_minutes"]:
                await _close_position(pos, remaining,
                                      f"Timeout {age_min:.0f}min ({pnl_pct:+.1f}%)", pnl_pct, config)
                continue

        except Exception as e:
            _log.debug(f"Position manage error {pos.symbol}: {e}")


async def _close_position(pos: SnipePosition, sell_pct: float, reason: str,
                          pnl_pct: float, config: dict):
    """Sell part or all of a snipe position."""
    # sell_pct is % of ORIGINAL position, tokens_held tracks actual remaining tokens
    actual_remaining = float(pos.remaining_pct)
    sell_fraction = sell_pct / actual_remaining if actual_remaining > 0 else 1
    tokens_to_sell = float(pos.tokens_held) * sell_fraction
    remaining = actual_remaining - sell_pct

    result = await _execute_sell(pos.mint, tokens_to_sell, config)
    mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

    async with async_session() as db:
        p = await db.get(SnipePosition, pos.id)
        if p:
            p.remaining_pct = Decimal(str(max(remaining, 0)))
            p.tokens_held = Decimal(str(max(float(p.tokens_held) - tokens_to_sell, 0)))
            if remaining <= 0:
                p.status = "CLOSED"
                p.close_reason = reason
                p.closed_at = datetime.utcnow()

            price_sol = float(p.current_price_sol or p.entry_price_sol)
            sol_value = float(p.position_size_sol) * (sell_pct / 100) * (1 + pnl_pct / 100)

            trade = SnipeTrade(
                position_id=pos.id, action="SELL",
                price_sol=Decimal(str(price_sol)),
                amount_sol=Decimal(str(round(sol_value, 6))),
                pnl_pct=Decimal(str(round(pnl_pct, 4))),
                reason=reason[:100],
                tx_hash=result.get("tx_hash"),
            )
            db.add(trade)
            await db.commit()

        if pnl_pct < 0 and remaining <= 0:
            loss = float(pos.position_size_sol) * abs(pnl_pct) / 100
            await _track_daily_loss(loss)

    emoji = "🟢" if pnl_pct > 0 else "🔴"
    _log.info(f"{mode} SNIPE SELL: ${pos.symbol} {sell_pct:.0f}% @ {pnl_pct:+.1f}% — {reason}")
    await _send_telegram(
        f"{emoji} *{mode} SNIPE SELL: ${pos.symbol}*\n"
        f"{reason}\n"
        f"Sold {sell_pct:.0f}%, PnL: {pnl_pct:+.1f}%"
    )


# === WEBSOCKET LOOP ===

async def _run_websocket(config: dict):
    """Connect to PumpPortal WebSocket and process events."""
    import websockets

    while True:
        try:
            async with websockets.connect(PUMPPORTAL_WS, ping_interval=30) as ws:
                _log.info("PumpPortal WebSocket connected")

                # Subscribe to new tokens and migrations
                await ws.send(_json.dumps({"method": "subscribeNewToken"}))

                # Subscribe to trades for open positions
                async with async_session() as db:
                    open_positions = (await db.execute(
                        select(SnipePosition).where(SnipePosition.status == "OPEN")
                    )).scalars().all()
                    for pos in open_positions:
                        await ws.send(_json.dumps({
                            "method": "subscribeTokenTrade",
                            "keys": [pos.mint],
                        }))

                async for message in ws:
                    try:
                        data = _json.loads(message)
                        tx_type = data.get("txType", "")

                        if tx_type == "create":
                            # New token created — track it for curve monitoring
                            mint = data.get("mint", "")
                            if mint:
                                _tracked_tokens[mint] = {
                                    "symbol": data.get("symbol", ""),
                                    "name": data.get("name", ""),
                                    "dev": data.get("traderPublicKey", ""),
                                    "v_sol": float(data.get("vSolInBondingCurve", 0) or 0),
                                    "v_tokens": float(data.get("vTokensInBondingCurve", 0) or 0),
                                    "created_at": time.time(),
                                    "holders": set(),
                                    "buys": 0,
                                    "sells": 0,
                                    "volume_sol": float(data.get("initialBuy", 0) or 0),
                                }
                                # Subscribe to trades for this token
                                await ws.send(_json.dumps({
                                    "method": "subscribeTokenTrade",
                                    "keys": [mint],
                                }))

                        elif tx_type in ("buy", "sell"):
                            mint = data.get("mint", "")
                            if mint in _tracked_tokens:
                                t = _tracked_tokens[mint]
                                t["v_sol"] = float(data.get("vSolInBondingCurve", 0) or 0)
                                t["v_tokens"] = float(data.get("vTokensInBondingCurve", 0) or 0)
                                sol_amount = float(data.get("solAmount", 0) or data.get("sol_amount", 0) or 0)
                                trader = data.get("traderPublicKey", "")

                                if tx_type == "buy":
                                    t["buys"] = t.get("buys", 0) + 1
                                    t["volume_sol"] = t.get("volume_sol", 0) + sol_amount
                                    if trader:
                                        if isinstance(t.get("holders"), set):
                                            t["holders"].add(trader)
                                else:
                                    t["sells"] = t.get("sells", 0) + 1

                                # Check if this token now meets entry criteria
                                holder_count = len(t["holders"]) if isinstance(t.get("holders"), set) else 0
                                eval_data = {
                                    "mint": mint,
                                    "symbol": t["symbol"],
                                    "name": t.get("name", ""),
                                    "vSolInBondingCurve": t["v_sol"],
                                    "vTokensInBondingCurve": t["v_tokens"],
                                    "holders": holder_count,
                                    "volume_sol": t.get("volume_sol", 0),
                                    "buys": t.get("buys", 0),
                                    "sells": t.get("sells", 0),
                                    "traderPublicKey": t.get("dev", ""),
                                    "dev_pct": 0,  # TODO: check on-chain
                                    "created_at": t.get("created_at"),
                                }

                                config = await get_config()
                                new_mint = await _evaluate_token(eval_data, config)
                                if new_mint:
                                    await ws.send(_json.dumps({
                                        "method": "subscribeTokenTrade",
                                        "keys": [new_mint],
                                    }))

                    except _json.JSONDecodeError:
                        pass
                    except Exception as e:
                        _log.debug(f"WS message error: {e}")

                    # Manage positions every ~10 seconds (not every message)
                    now_sec = int(time.time())
                    if now_sec % 10 == 0 and not hasattr(_run_websocket, '_last_manage') or \
                       now_sec - getattr(_run_websocket, '_last_manage', 0) >= 10:
                        _run_websocket._last_manage = now_sec
                        config = await get_config()
                        await _manage_positions(config)

                    # Trim tracked tokens — evict oldest, keep open position mints
                    if len(_tracked_tokens) > 1000:
                        async with async_session() as db:
                            open_mints = set((await db.execute(
                                select(SnipePosition.mint).where(SnipePosition.status == "OPEN")
                            )).scalars().all())
                        oldest = sorted(_tracked_tokens.items(),
                                        key=lambda x: x[1].get("created_at", 0))[:500]
                        for mint, _ in oldest:
                            if mint not in open_mints:
                                del _tracked_tokens[mint]

        except Exception as e:
            _log.error(f"PumpPortal WebSocket error: {e}")
            await asyncio.sleep(5)


# === MAIN LOOP ===

async def run():
    _log.info("Pump.fun Graduation Sniper starting")
    await asyncio.sleep(45)

    # Load previously traded mints to prevent re-entry after restart
    try:
        async with async_session() as db:
            all_mints = (await db.execute(select(SnipePosition.mint))).scalars().all()
            _seen_mints.update(all_mints)
            _log.info(f"Loaded {len(_seen_mints)} previously traded mints")
    except Exception:
        pass

    config = await get_config()
    mode = "PAPER MODE" if config.get("paper_mode", True) else "LIVE MODE"
    _log.info(f"Sniper: {mode} — size={config['position_size_sol']} SOL, "
              f"max={config['max_open_positions']}, curve={config['min_curve_pct']}-{config['max_curve_pct']}%, "
              f"SL={config['stop_loss_pct']}%, TP1={config['tp1_pct']}%, TP2={config['tp2_pct']}%")

    if not config.get("enabled"):
        _log.info("Sniper disabled in config")
        while True:
            await asyncio.sleep(300)

    await _run_websocket(config)

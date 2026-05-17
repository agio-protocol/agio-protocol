# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Whale Follow Bot — tracks hundreds of proven wallets, enters on large buys.

Strategy: Cast a wide net across 100-500 proven profitable wallets.
When any of them drops $900+ on a token, follow them in.
Exit when THEY exit. Simple. Follow the money.

Detection: Helius WebSocket logsSubscribe for all tracked wallets
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
import websockets
from sqlalchemy import select, func, String, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("whale-follow")

WHALE_WALLET_KEY_ENV = "WHALE_FOLLOW_PRIVATE_KEY"
SOL_MINT = "So11111111111111111111111111111111111111112"

DEFAULT_CONFIG = {
    "enabled": True,
    "paper_mode": True,

    # Position sizing — scale with whale's conviction
    "min_buy_usd": 500,
    "size_900": 0.15,
    "size_2000": 0.25,
    "size_5000": 0.40,
    "max_open_positions": 8,
    "daily_loss_limit_sol": 0.50,

    # Exit — ratcheting TP + trailing stop (data-driven)
    "tp1_pct": 15,
    "tp1_sell_pct": 50,
    "trailing_stop_pct": 15,
    "hard_stop_pct": 25,
    "max_hold_hours": 4,
    "follow_whale_exit": False,

    # Filters — small cap only (large cap = dead money per data)
    "min_wallet_winrate": 0.50,
    "min_wallet_profit_usd": 50000,
    "min_mc_usd": 10000,
    "max_mc_usd": 25000000,
    "cooldown_hours": 4,
    "skip_symbols": ["WSOL", "WETH", "WBTC", "USDC", "USDT", "SOL"],
}


async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("whale_follow_config")
        if stored:
            return {**DEFAULT_CONFIG, **_json.loads(stored)}
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


# === DB MODELS ===

class WhalePosition(Base):
    __tablename__ = "whale_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    whale_wallet: Mapped[str] = mapped_column(String(66), nullable=False)
    whale_buy_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    entry_price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    entry_mc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    position_size_sol: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    close_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    remaining_pct: Mapped[float] = mapped_column(Numeric(8, 4), default=100.0)
    tp1_done: Mapped[bool] = mapped_column(Boolean, default=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_whale_status", "status"),
        Index("idx_whale_token", "token_address"),
        Index("idx_whale_wallet", "whale_wallet"),
    )


class WhaleTrade(Base):
    __tablename__ = "whale_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    amount_sol: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TrackedWhale(Base):
    __tablename__ = "whale_tracked_wallets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(66), nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    winrate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    realized_profit: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    total_trades: Mapped[int | None] = mapped_column(BigInteger, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# === STATE ===
_daily_loss = 0.0
_daily_loss_date = ""
_seen_sigs: dict[str, float] = {}
_traded_tokens: dict[str, float] = {}
_evaluating: set[str] = set()  # prevents concurrent entry on same token


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


def _get_position_size(buy_usd: float, config: dict) -> float:
    if buy_usd >= 5000:
        return config.get("size_5000", 0.40)
    elif buy_usd >= 2000:
        return config.get("size_2000", 0.25)
    return config.get("size_900", 0.15)


# === WALLET DISCOVERY ===

async def _discover_whales(config: dict):
    """Populate whale list from GMGN smart money API."""
    try:
        from ..services.gmgn_client import get_smart_money_trades
        data = await get_smart_money_trades(limit=100)
        if not data:
            return

        items = data.get("data", data)
        if isinstance(items, dict):
            items = items.get("list", [])
        if not isinstance(items, list):
            return

        min_wr = config.get("min_wallet_winrate", 0.50)
        added = 0

        async with async_session() as db:
            for item in items:
                addr = item.get("maker", "") or item.get("wallet_address", "")
                if not addr:
                    continue

                existing = (await db.execute(
                    select(TrackedWhale).where(TrackedWhale.address == addr)
                )).scalar_one_or_none()
                if existing:
                    continue

                maker_info = item.get("maker_info", {})
                if not isinstance(maker_info, dict):
                    continue

                tags = maker_info.get("tags", [])
                if isinstance(tags, list) and any(t in ["sandwich_bot", "mev_bot"] for t in tags):
                    continue

                label = maker_info.get("twitter_username") or maker_info.get("name") or addr[:12]
                whale = TrackedWhale(address=addr, label=label)
                db.add(whale)
                added += 1

            await db.commit()

        if added > 0:
            _log.info(f"Discovered {added} new whale wallets")

    except Exception as e:
        _log.debug(f"Whale discovery error: {e}")


async def _get_tracked_whales() -> list[TrackedWhale]:
    async with async_session() as db:
        whales = (await db.execute(
            select(TrackedWhale).where(TrackedWhale.active == True)
        )).scalars().all()
        return list(whales)


# === ENTRY ===

async def _handle_whale_buy(wallet_addr: str, token_addr: str, buy_usd: float,
                            tx_hash: str, config: dict):
    """A tracked whale just made a large buy. Evaluate and enter."""
    # Dedup gate — synchronous check + claim, no await between
    if token_addr in _evaluating:
        return
    if token_addr in _traded_tokens:
        cooldown = config.get("cooldown_hours", 4) * 3600
        if time.time() - _traded_tokens[token_addr] < cooldown:
            return
    _evaluating.add(token_addr)

    try:
        daily = await _get_daily_loss()
        if daily >= config["daily_loss_limit_sol"]:
            return

        # Get token info
        symbol = "???"
        price = 0
        mc = 0
        try:
            from ..services.price_cache import get_pair_data
            p = await get_pair_data(token_addr)
            if p:
                symbol = p.get("baseToken", {}).get("symbol", "???")[:20]
                price = float(p.get("priceUsd", 0) or 0)
                mc = float(p.get("fdv", 0) or p.get("marketCap", 0) or 0)
                m5 = float(p.get("priceChange", {}).get("m5", 0) or 0)
                if m5 < -15:
                    _log.info(f"SKIP ${symbol}: dumping 5m={m5:.0f}%")
                    return
        except Exception as e:
            _log.debug(f"Price cache error for {token_addr[:16]}: {e}")

        # Fallback: try pump.fun API for new tokens not yet on DexScreener
        if price <= 0:
            try:
                async with httpx.AsyncClient() as client:
                    pf_resp = await client.get(
                        f"https://frontend-api-v3.pump.fun/coins/{token_addr}",
                        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                        timeout=5)
                    if pf_resp.status_code == 200 and pf_resp.content:
                        pf = pf_resp.json()
                        if pf.get("mint") == token_addr:
                            symbol = pf.get("symbol", "???")[:20]
                            usd_mc = float(pf.get("usd_market_cap", 0) or 0)
                            mc = usd_mc
                            sol_price_cached = await _get_cached_sol_price()
                            real_sol = float(pf.get("real_sol_reserves", 0) or 0) / 1e9
                            v_tokens = float(pf.get("virtual_token_reserves", 0) or 0) / 1e6
                            if v_tokens > 0 and real_sol > 0:
                                price = (real_sol + 30) / v_tokens * sol_price_cached
                            _log.info(f"Pump.fun fallback: ${symbol} MC=${mc:,.0f} price=${price:.8f}")
            except Exception as e:
                _log.debug(f"Pump.fun fallback error for {token_addr[:16]}: {e}")

        # If no price but we have MC from pump.fun, still apply MC filter
        if price <= 0 and mc <= 0:
            _log.info(f"No price/MC for {token_addr[:20]}... — entering on whale conviction (${buy_usd:,.0f})")

        skip = config.get("skip_symbols", [])
        if symbol.upper() in skip:
            _log.info(f"SKIP ${symbol}: in skip list")
            return

        if mc > 0 and (mc < config.get("min_mc_usd", 100000) or mc > config.get("max_mc_usd", 10000000)):
            _log.info(f"SKIP ${symbol}: MC ${mc:,.0f} out of range")
            return

        async with async_session() as db:
            open_count = (await db.execute(
                select(func.count()).select_from(WhalePosition)
                .where(WhalePosition.status == "OPEN")
            )).scalar() or 0
            if open_count >= config["max_open_positions"]:
                _log.info(f"SKIP ${symbol}: {open_count} open positions >= max {config['max_open_positions']}")
                return

            ever = (await db.execute(
                select(func.count()).select_from(WhalePosition)
                .where(WhalePosition.token_address == token_addr,
                       WhalePosition.opened_at >= datetime.utcnow() - timedelta(hours=config.get("cooldown_hours", 4)))
            )).scalar() or 0
            if ever > 0:
                _log.info(f"SKIP ${symbol}: traded in last {config.get('cooldown_hours', 4)}h")
                return

        try:
            from ..services.gmgn_client import get_token_security
            sec = await get_token_security(token_addr)
            if sec:
                d = sec.get("data", {})
                if d.get("is_honeypot") == "yes":
                    _log.info(f"SKIP ${symbol}: honeypot")
                    return
                if float(d.get("sell_tax", 0) or 0) > 0.10:
                    _log.info(f"SKIP ${symbol}: high sell tax")
                    return
        except Exception:
            pass

        # ENTER
        _traded_tokens[token_addr] = time.time()
        position_sol = _get_position_size(buy_usd, config)
        mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

        # Live execution via Jupiter
        tx_hash = "paper_mode"
        if not config.get("paper_mode", True):
            try:
                import base64 as b64
                from solders.keypair import Keypair
                from solders.transaction import VersionedTransaction

                pk = os.getenv(WHALE_WALLET_KEY_ENV, "")
                if not pk:
                    _log.error(f"🐋 LIVE BUY FAILED: {WHALE_WALLET_KEY_ENV} not set")
                    return
                if pk.startswith("["):
                    keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
                else:
                    import base58 as b58
                    keypair = Keypair.from_bytes(b58.b58decode(pk))

                sol_mint = "So11111111111111111111111111111111111111112"
                amount_lamports = int(position_sol * 1e9)
                jupiter_api = "https://api.jup.ag/swap/v1"
                _hk = os.getenv("HELIUS_API_KEY", "")
                rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

                _log.info(f"🐋 LIVE BUY: ${symbol} executing {position_sol} SOL swap...")
                async with httpx.AsyncClient() as jup_client:
                    quote_resp = await jup_client.get(f"{jupiter_api}/quote", params={
                        "inputMint": sol_mint, "outputMint": token_addr,
                        "amount": str(amount_lamports), "slippageBps": 1500,
                    }, timeout=10)
                    if quote_resp.status_code != 200:
                        _log.warning(f"🐋 LIVE BUY FAILED: quote error {quote_resp.status_code}")
                        return
                    quote = quote_resp.json()

                    swap_resp = await jup_client.post(f"{jupiter_api}/swap", json={
                        "quoteResponse": quote,
                        "userPublicKey": str(keypair.pubkey()),
                        "wrapAndUnwrapSol": True,
                        "dynamicComputeUnitLimit": True,
                    }, timeout=15)
                    if swap_resp.status_code != 200:
                        _log.warning(f"🐋 LIVE BUY FAILED: swap error {swap_resp.status_code}")
                        return

                    swap_tx = swap_resp.json().get("swapTransaction")
                    if not swap_tx:
                        _log.warning("🐋 LIVE BUY FAILED: no swap tx")
                        return

                    tx = VersionedTransaction.from_bytes(b64.b64decode(swap_tx))
                    signed_tx = VersionedTransaction(tx.message, [keypair])

                    send_resp = await jup_client.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                        "params": [b64.b64encode(bytes(signed_tx)).decode(),
                                   {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}],
                    }, timeout=30)

                    if send_resp.status_code == 200 and "result" in send_resp.json():
                        tx_hash = send_resp.json()["result"]
                        _log.info(f"🐋 TX SENT: ${symbol} tx={tx_hash[:20]}... confirming...")

                        # Confirm transaction actually landed
                        confirmed = False
                        for _ in range(10):
                            await asyncio.sleep(2)
                            conf_resp = await jup_client.post(rpc, json={
                                "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                                "params": [[tx_hash], {"searchTransactionHistory": False}],
                            }, timeout=5)
                            if conf_resp.status_code == 200:
                                statuses = conf_resp.json().get("result", {}).get("value", [])
                                if statuses and statuses[0]:
                                    if statuses[0].get("err"):
                                        _log.warning(f"🐋 LIVE BUY FAILED: tx confirmed but errored: {statuses[0]['err']}")
                                        return
                                    if statuses[0].get("confirmationStatus") in ("confirmed", "finalized"):
                                        confirmed = True
                                        _log.info(f"🐋 LIVE BUY CONFIRMED: ${symbol} tx={tx_hash[:20]}...")
                                        break

                        if not confirmed:
                            _log.warning(f"🐋 LIVE BUY UNCONFIRMED: ${symbol} tx={tx_hash[:20]}... — not recording position")
                            return
                    else:
                        _log.warning(f"🐋 LIVE BUY FAILED: send error {send_resp.text[:100]}")
                        return
            except Exception as e:
                _log.error(f"🐋 LIVE BUY ERROR: ${symbol} — {e}")
                return

        # If no price from DexScreener, get real price AFTER buy by checking token balance
        if price <= 0 and not config.get("paper_mode", True) and tx_hash and tx_hash != "paper_mode":
            await asyncio.sleep(3)
            try:
                pk = os.getenv(WHALE_WALLET_KEY_ENV, "")
                if pk:
                    from solders.keypair import Keypair
                    import base58 as b58
                    if pk.startswith("["):
                        kp = Keypair.from_bytes(bytes(_json.loads(pk)))
                    else:
                        kp = Keypair.from_bytes(b58.b58decode(pk))
                    _hk = os.getenv("HELIUS_API_KEY", "")
                    _rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else "https://api.mainnet-beta.solana.com"
                    async with httpx.AsyncClient() as client:
                        for prog in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                                     "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
                            br = await client.post(_rpc, json={
                                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                                "params": [str(kp.pubkey()), {"programId": prog}, {"encoding": "jsonParsed"}],
                            }, timeout=8)
                            if br.status_code == 200:
                                for acc in br.json().get("result", {}).get("value", []):
                                    info = acc["account"]["data"]["parsed"]["info"]
                                    if info.get("mint") == token_addr:
                                        raw = int(info["tokenAmount"]["amount"])
                                        decimals = int(info["tokenAmount"]["decimals"])
                                        if raw > 0:
                                            tokens_received = raw / (10 ** decimals)
                                            sol_price_now = await _get_cached_sol_price()
                                            usd_spent = position_sol * sol_price_now
                                            price = usd_spent / tokens_received
                                            _log.info(f"Computed entry price: ${price:.10f} ({tokens_received:.0f} tokens for {position_sol} SOL)")
                                        break
                            if price > 0:
                                break
            except Exception as e:
                _log.debug(f"Entry price compute error: {e}")

        if price <= 0:
            price = 0.000001

        _log.info(f"🐋 {mode} WHALE BUY: ${symbol} — whale spent ${buy_usd:,.0f} — "
                  f"we enter {position_sol} SOL @ ${price:.10f} MC=${mc:,.0f}")

        async with async_session() as db:
            pos = WhalePosition(
                token_address=token_addr,
                token_symbol=symbol,
                whale_wallet=wallet_addr,
                whale_buy_usd=Decimal(str(round(buy_usd, 2))),
                entry_price=Decimal(str(price)),
                entry_mc=Decimal(str(mc)),
                position_size_sol=Decimal(str(position_sol)),
                current_price=Decimal(str(price)),
                highest_price=Decimal(str(price)),
            )
            db.add(pos)
            await db.flush()

            trade = WhaleTrade(
                position_id=pos.id, action="BUY",
                price=Decimal(str(price)),
                amount_sol=Decimal(str(position_sol)),
                reason=f"Whale {wallet_addr[:12]} bought ${buy_usd:,.0f}",
            )
            db.add(trade)
            await db.commit()

        await _send_telegram(
            f"🐋 *{mode} WHALE FOLLOW: ${symbol}*\n"
            f"Whale: `{wallet_addr[:16]}...`\n"
            f"Whale buy: ${buy_usd:,.0f}\n"
            f"Our size: {position_sol} SOL\n"
            f"MC: ${mc:,.0f}\n"
            f"[Chart](https://dexscreener.com/solana/{token_addr})"
        )
    finally:
        _evaluating.discard(token_addr)


async def _handle_whale_sell(wallet_addr: str, token_addr: str, config: dict):
    """A tracked whale just sold a token we hold."""
    async with async_session() as db:
        pos = (await db.execute(
            select(WhalePosition)
            .where(WhalePosition.whale_wallet == wallet_addr,
                   WhalePosition.token_address == token_addr,
                   WhalePosition.status == "OPEN")
        )).scalar_one_or_none()

        if pos:
            if not config.get("follow_whale_exit", False):
                _log.info(f"🐋 Whale {wallet_addr[:12]} sold ${pos.token_symbol} — ignoring (follow_whale_exit=False)")
                return

            price = 0
            try:
                async with httpx.AsyncClient() as client:
                    from ..services.price_cache import get_price
                    price, _ = await get_price(token_addr)
            except Exception:
                pass

            entry = float(pos.entry_price)
            pnl_pct = ((price - entry) / entry * 100) if entry > 0 and price > 0 else 0

            await _close_position(pos, f"Whale sold ({pnl_pct:+.1f}%)", pnl_pct, config)


# === POSITION MANAGEMENT (trailing stop only) ===

async def _manage_positions(config: dict):
    """Ratcheting TP + trailing stop. Data shows: take profit early, let winners run."""
    async with async_session() as db:
        positions = (await db.execute(
            select(WhalePosition).where(WhalePosition.status == "OPEN")
        )).scalars().all()

    for pos in positions:
        try:
            price = 0
            try:
                from ..services.price_cache import get_price
                price, _ = await get_price(pos.token_address)
            except Exception:
                pass

            # No price available — skip this tick, try again next cycle
            # Jupiter quote fallback in price_cache will find it if tradable
            if price <= 0:
                continue

            entry = float(pos.entry_price)
            pnl_pct = ((price - entry) / entry * 100) if entry > 0 else 0
            highest = max(float(pos.highest_price or price), price)
            highest_pnl = ((highest - entry) / entry * 100) if entry > 0 else 0
            remaining = float(pos.remaining_pct) if pos.remaining_pct else 100.0
            tp1_done = bool(pos.tp1_done) if pos.tp1_done else False
            age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600

            async with async_session() as db:
                p = await db.get(WhalePosition, pos.id)
                if p:
                    p.current_price = Decimal(str(price))
                    p.highest_price = Decimal(str(highest))
                    p.pnl_pct = Decimal(str(round(pnl_pct, 4)))
                    await db.commit()

            # === HARD STOP — limit downside ===
            if pnl_pct <= -config["hard_stop_pct"]:
                await _close_position(pos, f"Hard stop {pnl_pct:.1f}%", pnl_pct, config)
                continue

            # === MAX HOLD TIMEOUT ===
            max_hold = config.get("max_hold_hours", 4)
            if age_hours >= max_hold:
                await _close_position(pos,
                    f"Max hold {age_hours:.1f}h ({pnl_pct:+.1f}%)", pnl_pct, config)
                continue

            # === TP1 — sell 50% at +15% to lock in profit ===
            tp1_pct = config.get("tp1_pct", 15)
            tp1_sell = config.get("tp1_sell_pct", 50)
            if pnl_pct >= tp1_pct and not tp1_done:
                _log.info(f"💰 TP1 HIT: ${pos.token_symbol} pnl={pnl_pct:+.1f}% — selling {tp1_sell}%")
                await _close_partial(pos, tp1_sell,
                    f"TP1 +{pnl_pct:.0f}% (sell {tp1_sell}%)", pnl_pct, config)
                continue

            # === TRAILING STOP — active after TP1, trails from peak ===
            trail_pct = config["trailing_stop_pct"]
            trail_stop = highest * (1 - trail_pct / 100)
            if price <= trail_stop and highest_pnl >= tp1_pct:
                await _close_position(pos,
                    f"Trail stop ({pnl_pct:+.1f}%, peak +{highest_pnl:.0f}%)",
                    pnl_pct, config)
                continue

        except Exception as e:
            _log.debug(f"Manage error {pos.token_symbol}: {e}")


async def _close_partial(pos: WhalePosition, sell_pct: float, reason: str,
                         pnl_pct: float, config: dict):
    """Sell a portion of the position (TP1). Updates remaining_pct and tp1_done."""
    mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

    if not config.get("paper_mode", True):
        try:
            import base64 as b64
            from solders.keypair import Keypair
            from solders.transaction import VersionedTransaction

            pk = os.getenv(WHALE_WALLET_KEY_ENV, "")
            if pk:
                if pk.startswith("["):
                    keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
                else:
                    import base58 as b58
                    keypair = Keypair.from_bytes(b58.b58decode(pk))

                sol_mint = "So11111111111111111111111111111111111111112"
                jupiter_api = "https://api.jup.ag/swap/v1"
                _hk = os.getenv("HELIUS_API_KEY", "")
                rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

                async with httpx.AsyncClient() as client:
                    raw_amount = 0
                    for prog in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                                 "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
                        try:
                            bal_resp = await client.post(rpc, json={
                                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                                "params": [str(keypair.pubkey()), {"programId": prog}, {"encoding": "jsonParsed"}],
                            }, timeout=8)
                            if bal_resp.status_code == 200:
                                for acc in bal_resp.json().get("result", {}).get("value", []):
                                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                                    if info.get("mint") == pos.token_address:
                                        raw_amount = max(raw_amount, int(info.get("tokenAmount", {}).get("amount", 0)))
                        except Exception:
                            pass
                        if raw_amount > 0:
                            break

                    sell_amount = int(raw_amount * sell_pct / 100)
                    if sell_amount > 0:
                        _log.info(f"💰 LIVE TP1 SELL: ${pos.token_symbol} selling {sell_pct}%...")
                        quote_resp = await client.get(f"{jupiter_api}/quote", params={
                            "inputMint": pos.token_address, "outputMint": sol_mint,
                            "amount": str(sell_amount), "slippageBps": 1500,
                        }, timeout=10)
                        if quote_resp.status_code == 200:
                            swap_resp = await client.post(f"{jupiter_api}/swap", json={
                                "quoteResponse": quote_resp.json(),
                                "userPublicKey": str(keypair.pubkey()),
                                "wrapAndUnwrapSol": True,
                                "dynamicComputeUnitLimit": True,
                            }, timeout=15)
                            if swap_resp.status_code == 200:
                                swap_tx = swap_resp.json().get("swapTransaction")
                                if swap_tx:
                                    tx = VersionedTransaction.from_bytes(b64.b64decode(swap_tx))
                                    signed_tx = VersionedTransaction(tx.message, [keypair])
                                    send_resp = await client.post(rpc, json={
                                        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                                        "params": [b64.b64encode(bytes(signed_tx)).decode(),
                                                   {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}],
                                    }, timeout=30)
                                    if send_resp.status_code == 200 and "result" in send_resp.json():
                                        _log.info(f"💰 LIVE TP1 SUCCESS: ${pos.token_symbol} tx={send_resp.json()['result'][:20]}...")
        except Exception as e:
            _log.error(f"💰 LIVE TP1 ERROR: ${pos.token_symbol} — {e}")

    async with async_session() as db:
        p = await db.get(WhalePosition, pos.id)
        if p:
            p.remaining_pct = Decimal(str(max(float(p.remaining_pct or 100) - sell_pct, 0)))
            p.tp1_done = True

            trade = WhaleTrade(
                position_id=pos.id, action="SELL",
                price=Decimal(str(float(p.current_price or p.entry_price))),
                amount_sol=Decimal(str(float(p.position_size_sol) * sell_pct / 100)),
                pnl_pct=Decimal(str(round(pnl_pct, 4))),
                reason=reason[:100],
            )
            db.add(trade)
            await db.commit()

    _log.info(f"💰 {mode} TP1: ${pos.token_symbol} sold {sell_pct}% @ {pnl_pct:+.1f}% — remaining {100-sell_pct}%")
    await _send_telegram(
        f"💰 *{mode} TP1: ${pos.token_symbol}*\nSold {sell_pct}% @ {pnl_pct:+.1f}%\nRemaining: {100-sell_pct}% riding"
    )


async def _close_position(pos: WhalePosition, reason: str, pnl_pct: float, config: dict):
    mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

    # Live sell via Jupiter
    if not config.get("paper_mode", True):
        try:
            import base64 as b64
            from solders.keypair import Keypair
            from solders.transaction import VersionedTransaction

            pk = os.getenv(WHALE_WALLET_KEY_ENV, "")
            if pk:
                if pk.startswith("["):
                    keypair = Keypair.from_bytes(bytes(_json.loads(pk)))
                else:
                    import base58 as b58
                    keypair = Keypair.from_bytes(b58.b58decode(pk))

                sol_mint = "So11111111111111111111111111111111111111112"
                jupiter_api = "https://api.jup.ag/swap/v1"
                _hk = os.getenv("HELIUS_API_KEY", "")
                rpc = f"https://mainnet.helius-rpc.com/?api-key={_hk}" if _hk else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

                async with httpx.AsyncClient() as client:
                    raw_amount = 0
                    for prog in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                                 "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
                        try:
                            bal_resp = await client.post(rpc, json={
                                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                                "params": [str(keypair.pubkey()), {"programId": prog}, {"encoding": "jsonParsed"}],
                            }, timeout=8)
                            if bal_resp.status_code == 200:
                                for acc in bal_resp.json().get("result", {}).get("value", []):
                                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                                    if info.get("mint") == pos.token_address:
                                        raw_amount = max(raw_amount, int(info.get("tokenAmount", {}).get("amount", 0)))
                        except Exception:
                            pass
                        if raw_amount > 0:
                            break

                    if raw_amount > 0:
                        _log.info(f"🐋 LIVE SELL: ${pos.token_symbol} selling {raw_amount} tokens...")
                        quote_resp = await client.get(f"{jupiter_api}/quote", params={
                            "inputMint": pos.token_address, "outputMint": sol_mint,
                            "amount": str(raw_amount), "slippageBps": 1500,
                        }, timeout=10)
                        if quote_resp.status_code == 200:
                            swap_resp = await client.post(f"{jupiter_api}/swap", json={
                                "quoteResponse": quote_resp.json(),
                                "userPublicKey": str(keypair.pubkey()),
                                "wrapAndUnwrapSol": True,
                                "dynamicComputeUnitLimit": True,
                            }, timeout=15)
                            if swap_resp.status_code == 200:
                                swap_tx = swap_resp.json().get("swapTransaction")
                                if swap_tx:
                                    tx = VersionedTransaction.from_bytes(b64.b64decode(swap_tx))
                                    signed_tx = VersionedTransaction(tx.message, [keypair])
                                    send_resp = await client.post(rpc, json={
                                        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                                        "params": [b64.b64encode(bytes(signed_tx)).decode(),
                                                   {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}],
                                    }, timeout=30)
                                    if send_resp.status_code == 200 and "result" in send_resp.json():
                                        tx_hash = send_resp.json()["result"]
                                        _log.info(f"🐋 LIVE SELL SUCCESS: ${pos.token_symbol} tx={tx_hash[:20]}...")
                                    else:
                                        _log.warning(f"🐋 LIVE SELL FAILED: ${pos.token_symbol} send error")
                    else:
                        _log.warning(f"🐋 LIVE SELL: ${pos.token_symbol} no token balance found")
        except Exception as e:
            _log.error(f"🐋 LIVE SELL ERROR: ${pos.token_symbol} — {e}")

    async with async_session() as db:
        p = await db.get(WhalePosition, pos.id)
        if p:
            p.status = "CLOSED"
            p.close_reason = reason
            p.closed_at = datetime.utcnow()
            p.pnl_pct = Decimal(str(round(pnl_pct, 4)))

            trade = WhaleTrade(
                position_id=pos.id, action="SELL",
                price=Decimal(str(float(p.current_price or p.entry_price))),
                pnl_pct=Decimal(str(round(pnl_pct, 4))),
                reason=reason[:100],
            )
            db.add(trade)
            await db.commit()

    if pnl_pct < 0:
        loss = float(pos.position_size_sol) * abs(pnl_pct) / 100
        await _track_daily_loss(loss)

    emoji = "🟢" if pnl_pct > 0 else "🔴"
    _log.info(f"{emoji} {mode} WHALE CLOSE: ${pos.token_symbol} @ {pnl_pct:+.1f}% — {reason}")
    await _send_telegram(
        f"{emoji} *{mode} WHALE CLOSE: ${pos.token_symbol}*\n{reason}\nPnL: {pnl_pct:+.1f}%"
    )


# === HELIUS WEBSOCKET MONITOR ===

_api_semaphore = asyncio.Semaphore(5)  # Allow 5 concurrent tx parses for 431 wallets
_sol_price_cache = {"price": 150, "ts": 0}


async def _get_cached_sol_price() -> float:
    if time.time() - _sol_price_cache["ts"] > 60:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=3)
                if r.status_code == 200:
                    _sol_price_cache["price"] = r.json().get("solana", {}).get("usd", 150)
                    _sol_price_cache["ts"] = time.time()
        except Exception:
            pass
    return _sol_price_cache["price"]


async def _process_swap(sig: str, wallet_addr: str, label: str,
                       helius_key: str, config: dict):
    """Process a detected swap — standard RPC getTransaction (1 credit, not 100)."""
    await asyncio.sleep(3.0)
    async with _api_semaphore:
        try:
            rpc = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
            async with httpx.AsyncClient() as client:
                result = None
                for attempt in range(3):
                    tx_resp = await client.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                        "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                         "commitment": "confirmed"}],
                    }, timeout=8)
                    if tx_resp.status_code != 200:
                        _log.info(f"getTransaction {tx_resp.status_code} for {label}")
                        return
                    result = tx_resp.json().get("result")
                    if result:
                        break
                    await asyncio.sleep(2.0)

                if not result:
                    _log.info(f"TX null for {label}: {sig[:20]}")
                    return
                meta = result.get("meta", {})
                if meta.get("err"):
                    _log.info(f"TX err for {label}: {meta.get('err')}")
                    return

                token_addr = ""
                side = ""
                sol_amount = 0

                SKIP_MINTS = {
                    SOL_MINT,
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
                    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",
                }

                # Parse token balance changes for this wallet
                pre_tokens = {}
                post_tokens = {}
                for tb in meta.get("preTokenBalances", []):
                    if tb.get("owner") == wallet_addr:
                        m = tb.get("mint", "")
                        if m:
                            pre_tokens[m] = int(tb.get("uiTokenAmount", {}).get("amount", "0") or "0")
                for tb in meta.get("postTokenBalances", []):
                    if tb.get("owner") == wallet_addr:
                        m = tb.get("mint", "")
                        if m:
                            post_tokens[m] = int(tb.get("uiTokenAmount", {}).get("amount", "0") or "0")

                for m in set(pre_tokens.keys()) | set(post_tokens.keys()):
                    if m in SKIP_MINTS:
                        continue
                    pre = pre_tokens.get(m, 0)
                    post = post_tokens.get(m, 0)
                    if post > pre:
                        side = "buy"
                        token_addr = m
                        break
                    elif pre > post and pre > 0:
                        side = "sell"
                        token_addr = m
                        break

                if side:
                    keys = result.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    pre_b = meta.get("preBalances", [])
                    post_b = meta.get("postBalances", [])

                    for i, key in enumerate(keys):
                        k = key.get("pubkey", key) if isinstance(key, dict) else str(key)
                        if k == wallet_addr and i < len(pre_b) and i < len(post_b):
                            sol_amount = abs(post_b[i] - pre_b[i]) / 1e9
                            break

                    if sol_amount < 0.1:
                        wsol_pre = pre_tokens.get(SOL_MINT, 0)
                        wsol_post = post_tokens.get(SOL_MINT, 0)
                        wsol_change = abs(wsol_post - wsol_pre) / 1e9
                        if wsol_change > sol_amount:
                            sol_amount = wsol_change

                if not token_addr or not side:
                    return

                sol_price = await _get_cached_sol_price()
                usd_value = sol_amount * sol_price

                # If SOL amount is tiny, estimate USD from token amount received
                if usd_value < 50 and side == "buy" and token_addr:
                    try:
                        from ..services.price_cache import get_price
                        token_price_usd, _ = await get_price(token_addr)
                        if token_price_usd > 0:
                            token_change = post_tokens.get(token_addr, 0) - pre_tokens.get(token_addr, 0)
                            if token_change > 0:
                                for tb in meta.get("postTokenBalances", []):
                                    if tb.get("mint") == token_addr:
                                        decimals = int(tb.get("uiTokenAmount", {}).get("decimals", 6))
                                        token_usd = (token_change / (10 ** decimals)) * token_price_usd
                                        if token_usd > usd_value:
                                            usd_value = token_usd
                                            sol_amount = usd_value / sol_price
                                        break
                    except Exception:
                        pass

                _log.info(f"PARSED: {label} {side} {sol_amount:.2f} SOL (${usd_value:,.0f}) token={token_addr[:16]}...")
                usd_value = sol_amount * sol_price

                if side == "buy":
                    if usd_value >= config.get("min_buy_usd", 850):
                        _log.info(f"🐋 WHALE DETECTED: {label} bought ${usd_value:,.0f} "
                                  f"({sol_amount:.2f} SOL) token={token_addr[:16]}...")
                        await _send_telegram(
                            f"🐋 *WHALE ALERT*\n"
                            f"Wallet: `{label}`\n"
                            f"Buy: *${usd_value:,.0f}* ({sol_amount:.2f} SOL)\n"
                            f"Token: `{token_addr}`\n"
                            f"[DexScreener](https://dexscreener.com/solana/{token_addr}) | "
                            f"[Solscan](https://solscan.io/token/{token_addr})"
                        )
                        await _handle_whale_buy(wallet_addr, token_addr, usd_value, sig, config)
                    elif usd_value > 100:
                        _log.info(f"Whale {label} buy ${usd_value:.0f} — below ${config.get('min_buy_usd', 850)} min")
                elif side == "sell":
                    await _handle_whale_sell(wallet_addr, token_addr, config)

        except Exception as e:
            _log.warning(f"TX parse error for {label}: {e}")


async def _run_single_ws(ws_url: str, wallet_chunk: list[tuple[str, str]],
                         helius_key: str, config: dict, chunk_id: int):
    """Run a single WebSocket connection monitoring a chunk of wallets."""
    while True:
        try:
            async with websockets.connect(
                ws_url, ping_interval=30, open_timeout=15, max_size=2**20,
            ) as ws:
                _log.info(f"WS#{chunk_id}: connected, subscribing to {len(wallet_chunk)} wallets")

                sub_map = {}
                pending_ids = {}
                for i, (addr, label) in enumerate(wallet_chunk):
                    req_id = chunk_id * 1000 + i + 1
                    pending_ids[req_id] = addr
                    await ws.send(_json.dumps({
                        "jsonrpc": "2.0", "id": req_id,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [addr]},
                            {"commitment": "confirmed"}
                        ]
                    }))
                    if (i + 1) % 10 == 0:
                        await asyncio.sleep(0.05)

                wallet_labels = {addr: label for addr, label in wallet_chunk}

                async for message in ws:
                    try:
                        data = _json.loads(message)

                        if "id" in data and "result" in data and isinstance(data["result"], int):
                            req_id = data["id"]
                            sub_id = data["result"]
                            if req_id in pending_ids:
                                sub_map[sub_id] = pending_ids.pop(req_id)
                                if len(pending_ids) == 0:
                                    _log.info(f"WS#{chunk_id}: all {len(sub_map)} subscriptions confirmed")

                        elif data.get("method") == "logsNotification":
                            params = data.get("params", {})
                            sub_id = params.get("subscription")
                            wallet_addr = sub_map.get(sub_id)
                            if not wallet_addr:
                                continue

                            value = params.get("result", {}).get("value", {})
                            sig = value.get("signature", "")
                            err = value.get("err")
                            logs = value.get("logs", [])

                            if err or not sig or sig in _seen_sigs:
                                continue

                            is_swap = any(
                                "Instruction: Swap" in log or
                                "Instruction: swap" in log or
                                "675kPX" in log or
                                "JUP" in log or
                                "whirLb" in log or
                                "6EF8rrecth" in log or
                                "LBUZKhRx" in log or
                                "CAMMCzo5" in log or
                                "PhoeNiXZ" in log or
                                "pAMMBay" in log or
                                "term9YPb" in log
                                for log in logs
                            )
                            if not is_swap:
                                continue

                            _seen_sigs[sig] = time.time()
                            label = wallet_labels.get(wallet_addr, wallet_addr[:12])

                            cfg = await get_config()
                            asyncio.create_task(
                                _process_swap(sig, wallet_addr, label, helius_key, cfg))

                    except _json.JSONDecodeError:
                        pass
                    except Exception as e:
                        _log.debug(f"WS#{chunk_id} message error: {e}")

                if len(_seen_sigs) > 10000:
                    cutoff = time.time() - 3600
                    _seen_sigs.clear()

        except Exception as e:
            _log.error(f"WS#{chunk_id} error: {type(e).__name__}: {e}")
            await asyncio.sleep(5)


async def _run_whale_monitor(config: dict):
    """Monitor all tracked whale wallets via multiple Helius WebSocket connections.
    Splits wallets into chunks of 40 per connection to stay under subscription limits."""
    helius_key = os.getenv("HELIUS_API_KEY", "")
    if not helius_key:
        _log.error("No HELIUS_API_KEY — whale follow bot cannot run")
        return

    whales = await _get_tracked_whales()
    if not whales:
        _log.info("No tracked whales — discovering...")
        await _discover_whales(config)
        whales = await _get_tracked_whales()

    wallet_list = [(w.address, w.label or w.address[:12]) for w in whales]
    ws_url = f"wss://mainnet.helius-rpc.com/?api-key={helius_key}"

    # Split into chunks of 40 wallets per connection
    CHUNK_SIZE = 40
    chunks = [wallet_list[i:i+CHUNK_SIZE] for i in range(0, len(wallet_list), CHUNK_SIZE)]
    _log.info(f"Monitoring {len(wallet_list)} wallets across {len(chunks)} WebSocket connections "
              f"({CHUNK_SIZE} per connection)")

    tasks = [
        _run_single_ws(ws_url, chunk, helius_key, config, i)
        for i, chunk in enumerate(chunks)
    ]
    await asyncio.gather(*tasks)


# === MAIN LOOP ===

async def run():
    _log.info("Whale Follow Bot starting")
    await asyncio.sleep(5)

    config = await get_config()
    if not config.get("enabled"):
        _log.info("Whale follow bot disabled")
        while True:
            await asyncio.sleep(300)

    mode = "PAPER MODE" if config.get("paper_mode", True) else "LIVE MODE"
    _log.info(f"Whale Follow Bot: {mode} — min_buy=${config['min_buy_usd']}, "
              f"trail={config['trailing_stop_pct']}%, stop={config['hard_stop_pct']}%, "
              f"max={config['max_open_positions']}")

    # Discover whales on startup
    await _discover_whales(config)
    whales = await _get_tracked_whales()
    _log.info(f"Tracking {len(whales)} whale wallets")

    async def manage_loop():
        while True:
            try:
                cfg = await get_config()
                await _manage_positions(cfg)
            except Exception as e:
                _log.debug(f"Manage error: {e}")
            await asyncio.sleep(10)

    async def discover_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                await _discover_whales(await get_config())
            except Exception:
                pass

    await asyncio.gather(
        _run_whale_monitor(config),
        manage_loop(),
        discover_loop(),
    )

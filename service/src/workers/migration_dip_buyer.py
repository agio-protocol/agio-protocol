# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Migration Dip Buyer — buys the post-migration dump, rides the bounce.

Strategy: When a pump.fun token graduates to Raydium, the price dumps
as early holders exit. This bot waits for the dump to bottom, confirms
a reversal (+5% off the low), then enters. Rides the bounce with TPs
and trailing stop.

Detection: Program monitor (logsSubscribe on pump.fun program)
Timing: DexScreener price polling every 2 seconds
Entry: Confirmed reversal (+5% from post-migration low)
Exit: TP1 at 2x, TP2 at 3x sell 50%, 25% trailing stop
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

_log = logging.getLogger("dip-buyer")

DEFAULT_CONFIG = {
    "enabled": True,
    "paper_mode": True,

    "position_size_sol": 0.15,
    "max_open_positions": 5,
    "daily_loss_limit_sol": 0.50,

    # Dip buy timing
    "reversal_pct": 5,
    "min_dump_pct": 5,
    "max_dump_pct": 60,
    "max_wait_seconds": 120,
    "poll_interval_seconds": 2,

    # Exit — ride the bounce
    "tp1_pct": 100,
    "tp1_sell_pct": 50,
    "tp2_pct": 200,
    "tp2_sell_pct": 50,
    "trailing_stop_pct": 25,
    "hard_stop_pct": 25,

    # Filters
    "min_holders": 50,
    "min_pre_migration_mc": 15000,
    "cooldown_hours": 4,
}


async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("dip_buyer_config")
        if stored:
            return {**DEFAULT_CONFIG, **_json.loads(stored)}
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


# === DB MODELS ===

class DipPosition(Base):
    __tablename__ = "dip_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    graduation_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    dump_low_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    dump_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    entry_price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    entry_mc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    position_size_sol: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    remaining_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    close_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_dip_status", "status"),
        Index("idx_dip_token", "token_address"),
    )


class DipTrade(Base):
    __tablename__ = "dip_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    amount_sol: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# === STATE ===
_daily_loss = 0.0
_daily_loss_date = ""
_seen_mints: set[str] = set()
_active_watches: dict[str, asyncio.Task] = {}


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


async def _get_price(mint: str) -> tuple[float, float]:
    """Get price and MC from DexScreener. Returns (price_usd, mc_usd)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if pairs:
                    return (float(pairs[0].get("priceUsd", 0) or 0),
                            float(pairs[0].get("fdv", 0) or 0))
    except Exception:
        pass
    return 0, 0


# === DIP WATCH — the core timing engine ===

async def _watch_for_dip_and_buy(mint: str, symbol: str, config: dict):
    """Watch a freshly graduated token for the dump bottom, then buy the reversal."""
    if mint in _seen_mints:
        return
    _seen_mints.add(mint)

    daily = await _get_daily_loss()
    if daily >= config["daily_loss_limit_sol"]:
        return

    async with async_session() as db:
        open_count = (await db.execute(
            select(func.count()).select_from(DipPosition)
            .where(DipPosition.status == "OPEN")
        )).scalar() or 0
        if open_count >= config["max_open_positions"]:
            return

    _log.info(f"👀 WATCHING: ${symbol} — waiting for post-migration dump...")

    # Wait a moment for Raydium pool to initialize
    await asyncio.sleep(5)

    # Get initial price (graduation price)
    grad_price, grad_mc = await _get_price(mint)
    if grad_price <= 0:
        await asyncio.sleep(3)
        grad_price, grad_mc = await _get_price(mint)
    if grad_price <= 0:
        _log.info(f"SKIP ${symbol}: no price after migration")
        return

    _log.info(f"👀 ${symbol} graduation price: ${grad_price:.6f} MC=${grad_mc:,.0f}")

    # Track the dump
    low_price = grad_price
    low_time = time.time()
    start_time = time.time()
    poll_interval = config.get("poll_interval_seconds", 2)
    max_wait = config.get("max_wait_seconds", 120)
    reversal_pct = config.get("reversal_pct", 5)
    min_dump = config.get("min_dump_pct", 5)
    max_dump = config.get("max_dump_pct", 60)

    while time.time() - start_time < max_wait:
        await asyncio.sleep(poll_interval)

        price, mc = await _get_price(mint)
        if price <= 0:
            continue

        # Track new low
        if price < low_price:
            low_price = price
            low_time = time.time()

        # Calculate dump from graduation price
        dump_pct = ((low_price - grad_price) / grad_price * 100) if grad_price > 0 else 0

        # Check if dump is too deep (rug)
        if dump_pct < -max_dump:
            _log.info(f"SKIP ${symbol}: dump too deep {dump_pct:.0f}% (rug)")
            return

        # Check for reversal: price bounced reversal_pct from the low
        if low_price > 0 and dump_pct < -min_dump:
            bounce_pct = ((price - low_price) / low_price * 100)
            if bounce_pct >= reversal_pct:
                elapsed = time.time() - start_time
                _log.info(f"🔄 REVERSAL: ${symbol} dumped {dump_pct:.0f}%, "
                          f"bounced +{bounce_pct:.0f}% from low ${low_price:.6f} "
                          f"→ ${price:.6f} in {elapsed:.0f}s")

                # Security check before buying
                safe = True
                try:
                    from ..services.gmgn_client import get_token_security
                    sec = await get_token_security(mint)
                    if sec:
                        d = sec.get("data", {})
                        if d.get("is_honeypot") == "yes":
                            safe = False
                        if float(d.get("sell_tax", 0) or 0) > 0.10:
                            safe = False
                except Exception:
                    pass

                if not safe:
                    _log.info(f"SKIP ${symbol}: failed security check")
                    return

                # BUY THE DIP
                mode = "PAPER" if config.get("paper_mode", True) else "LIVE"
                size = config["position_size_sol"]

                _log.info(f"🚀 DIP BUY: ${symbol} @ ${price:.6f} "
                          f"(dumped {dump_pct:.0f}%, bounced +{bounce_pct:.0f}%)")

                async with async_session() as db:
                    pos = DipPosition(
                        token_address=mint,
                        token_symbol=symbol,
                        graduation_price=Decimal(str(grad_price)),
                        dump_low_price=Decimal(str(low_price)),
                        dump_pct=Decimal(str(round(dump_pct, 4))),
                        entry_price=Decimal(str(price)),
                        entry_mc=Decimal(str(mc)),
                        position_size_sol=Decimal(str(size)),
                        current_price=Decimal(str(price)),
                        highest_price=Decimal(str(price)),
                    )
                    db.add(pos)
                    await db.flush()

                    trade = DipTrade(
                        position_id=pos.id, action="BUY",
                        price=Decimal(str(price)),
                        amount_sol=Decimal(str(size)),
                        reason=f"Dip buy: dumped {dump_pct:.0f}%, bounced +{bounce_pct:.0f}%",
                    )
                    db.add(trade)
                    await db.commit()

                await _send_telegram(
                    f"🔄 *{mode} DIP BUY: ${symbol}*\n"
                    f"Graduation: ${grad_price:.6f}\n"
                    f"Dump low: ${low_price:.6f} ({dump_pct:.0f}%)\n"
                    f"Entry: ${price:.6f} (bounced +{bounce_pct:.0f}%)\n"
                    f"Size: {size} SOL\n"
                    f"[Chart](https://dexscreener.com/solana/{mint})"
                )
                return

    _log.info(f"SKIP ${symbol}: no reversal in {max_wait}s")


# === POSITION MANAGEMENT ===

async def _manage_positions(config: dict):
    """TP1 at 2x, TP2 at 3x sell 50%, 25% trailing stop, 25% hard stop."""
    async with async_session() as db:
        positions = (await db.execute(
            select(DipPosition).where(DipPosition.status == "OPEN")
        )).scalars().all()

    for pos in positions:
        try:
            price, _ = await _get_price(pos.token_address)
            if price <= 0:
                continue

            entry = float(pos.entry_price)
            pnl_pct = ((price - entry) / entry * 100) if entry > 0 else 0
            highest = max(float(pos.highest_price or price), price)
            remaining = float(pos.remaining_pct)

            async with async_session() as db:
                p = await db.get(DipPosition, pos.id)
                if p:
                    p.current_price = Decimal(str(price))
                    p.highest_price = Decimal(str(highest))
                    p.pnl_pct = Decimal(str(round(pnl_pct, 4)))
                    await db.commit()

            # Hard stop
            if pnl_pct <= -config["hard_stop_pct"]:
                await _close_position(pos, remaining,
                    f"Hard stop {pnl_pct:.1f}%", pnl_pct, config)
                continue

            # TP1: +100% (2x) sell initial portion
            if pnl_pct >= config["tp1_pct"] and remaining > config["tp1_sell_pct"]:
                await _close_position(pos, config["tp1_sell_pct"],
                    f"TP1 +{pnl_pct:.0f}% (2x)", pnl_pct, config)
                continue

            # TP2: +200% (3x) sell 50% of remaining
            if pnl_pct >= config["tp2_pct"] and remaining > 25:
                sell = remaining * (config["tp2_sell_pct"] / 100)
                await _close_position(pos, sell,
                    f"TP2 +{pnl_pct:.0f}% (3x)", pnl_pct, config)
                continue

            # Trailing stop — 25% from peak
            trail_pct = config["trailing_stop_pct"]
            if highest > entry:
                trail_stop = highest * (1 - trail_pct / 100)
                if price <= trail_stop:
                    highest_pnl = ((highest - entry) / entry * 100)
                    await _close_position(pos, remaining,
                        f"Trail ({pnl_pct:+.1f}%, peak {highest_pnl:+.0f}%)",
                        pnl_pct, config)
                    continue

        except Exception as e:
            _log.debug(f"Manage error {pos.token_symbol}: {e}")


async def _close_position(pos: DipPosition, sell_pct: float, reason: str,
                          pnl_pct: float, config: dict):
    remaining = float(pos.remaining_pct) - sell_pct
    mode = "PAPER" if config.get("paper_mode", True) else "LIVE"

    async with async_session() as db:
        p = await db.get(DipPosition, pos.id)
        if p:
            p.remaining_pct = Decimal(str(max(remaining, 0)))
            if remaining <= 0:
                p.status = "CLOSED"
                p.close_reason = reason
                p.closed_at = datetime.utcnow()

            trade = DipTrade(
                position_id=pos.id, action="SELL",
                price=Decimal(str(float(p.current_price or p.entry_price))),
                pnl_pct=Decimal(str(round(pnl_pct, 4))),
                reason=reason[:100],
            )
            db.add(trade)
            await db.commit()

    if pnl_pct < 0 and remaining <= 0:
        loss = float(pos.position_size_sol) * abs(pnl_pct) / 100
        await _track_daily_loss(loss)

    emoji = "🟢" if pnl_pct > 0 else "🔴"
    _log.info(f"{emoji} {mode} DIP SELL: ${pos.token_symbol} {sell_pct:.0f}% @ {pnl_pct:+.1f}% — {reason}")
    await _send_telegram(
        f"{emoji} *{mode} DIP SELL: ${pos.token_symbol}*\n{reason}\nPnL: {pnl_pct:+.1f}%"
    )


# === GRADUATION HANDLER ===

async def on_graduation(mint: str, symbol: str):
    """Called by the program monitor when a token graduates."""
    config = await get_config()
    if not config.get("enabled"):
        return
    if mint in _active_watches:
        return

    # Launch a dip watch as a background task
    task = asyncio.create_task(_watch_for_dip_and_buy(mint, symbol, config))
    _active_watches[mint] = task

    # Clean up completed watches
    done = [m for m, t in _active_watches.items() if t.done()]
    for m in done:
        del _active_watches[m]


# === MAIN LOOP ===

async def run():
    _log.info("Migration Dip Buyer starting")
    await asyncio.sleep(5)

    config = await get_config()
    mode = "PAPER MODE" if config.get("paper_mode", True) else "LIVE MODE"
    _log.info(f"Dip Buyer: {mode} — size={config['position_size_sol']} SOL, "
              f"reversal={config['reversal_pct']}%, dump={config['min_dump_pct']}-{config['max_dump_pct']}%, "
              f"TP1=+{config['tp1_pct']}%, TP2=+{config['tp2_pct']}%, "
              f"trail={config['trailing_stop_pct']}%, stop={config['hard_stop_pct']}%")

    if not config.get("enabled"):
        _log.info("Dip buyer disabled")
        while True:
            await asyncio.sleep(300)

    # Load seen mints
    try:
        async with async_session() as db:
            mints = (await db.execute(select(DipPosition.token_address))).scalars().all()
            _seen_mints.update(mints)
    except Exception:
        pass

    # Subscribe to graduation events from Redis pub/sub
    # The migration sniper publishes to "graduation_events" channel
    async def graduation_listener():
        try:
            from ..core.redis import redis_client
            pubsub = redis_client.pubsub()
            await pubsub.subscribe("graduation_events")
            _log.info("Listening for graduation events via Redis pub/sub")

            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        data = _json.loads(message["data"])
                        mint = data.get("mint", "")
                        symbol = data.get("symbol", "?")
                        if mint:
                            _log.info(f"Graduation event: ${symbol} mint={mint[:16]}...")
                            await on_graduation(mint, symbol)
                    except Exception as e:
                        _log.debug(f"Graduation event parse error: {e}")
        except Exception as e:
            _log.error(f"Redis pub/sub error: {e}")
            # Fallback: poll migration_positions for new graduated entries
            _log.info("Falling back to DB polling for graduations")
            seen_graduated = set()
            while True:
                try:
                    async with async_session() as db:
                        from .migration_sniper import MigrationPosition
                        recent = (await db.execute(
                            select(MigrationPosition)
                            .where(MigrationPosition.migrated == True,
                                   MigrationPosition.opened_at >= datetime.utcnow() - timedelta(minutes=5))
                        )).scalars().all()
                        for pos in recent:
                            if pos.mint not in seen_graduated:
                                seen_graduated.add(pos.mint)
                                await on_graduation(pos.mint, pos.symbol or "?")
                except Exception:
                    pass
                await asyncio.sleep(5)

    async def manage_loop():
        while True:
            try:
                cfg = await get_config()
                await _manage_positions(cfg)
            except Exception as e:
                _log.debug(f"Manage error: {e}")
            await asyncio.sleep(5)

    _log.info("Starting graduation listener (Redis pub/sub + DB fallback)")

    await asyncio.gather(
        graduation_listener(),
        manage_loop(),
    )

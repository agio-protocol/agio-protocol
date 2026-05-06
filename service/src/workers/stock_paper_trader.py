# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Stock Paper Trading Bot — simulates stock trades using insider buying, Congress trades,
and options flow signals. Holds for days/weeks (swing-trade style).
All parameters adjustable via API without redeploying.
"""
import asyncio
import logging
import os
import json as _json
import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Float, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("stock-paper-trader")

POLL_INTERVAL = 300  # 5 minutes
ENTRY_CHECK_INTERVAL = 1800  # 30 minutes


# === DEFAULT CONFIG (adjustable via Redis) ===
DEFAULT_CONFIG = {
    "min_agiotage_score": 40,
    "min_sources": 2,           # require 2+ sources (insider + congress, or insider + options, etc.)
    "position_size_usd": 1000,
    "max_open_positions": 8,
    "max_holding_days": 20,
    "take_profit_levels": [
        {"sell_pct": 33, "at_profit_pct": 3},
        {"sell_pct": 33, "at_profit_pct": 8},
        {"sell_pct": 100, "at_profit_pct": 15},
    ],
    "stop_loss_pct": 5,
    "trailing_stop_enabled": True,
    "trailing_stop_activation_pct": 8,
    "trailing_stop_trail_pct": 3,
    "min_insider_value": 100000,   # minimum insider trade value to consider
    "require_buy_direction": True,  # only enter on BUY signals, not sells
}


# === DB MODELS ===

class StockPaperPosition(Base):
    __tablename__ = "stock_paper_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    side: Mapped[str] = mapped_column(String(10), default="long")
    entry_price: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    position_size_usd: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    remaining_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    current_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    entry_signal: Mapped[str | None] = mapped_column(String(30), nullable=True)
    entry_sources: Mapped[str | None] = mapped_column(Text, nullable=True)
    agiotage_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_spp_status", "status"),
        Index("idx_spp_opened", "opened_at"),
    )


class StockPaperTrade(Base):
    __tablename__ = "stock_paper_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    pct_of_position: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    usd_value: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_spt_position", "position_id"),
    )


# === CONFIG MANAGEMENT ===

async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("stock_paper_trader_config")
        if stored:
            return {**DEFAULT_CONFIG, **_json.loads(stored)}
    except:
        pass
    return DEFAULT_CONFIG.copy()


async def set_config(updates: dict):
    try:
        from ..core.redis import redis_client
        current = await get_config()
        current.update(updates)
        await redis_client.set("stock_paper_trader_config", _json.dumps(current))
    except:
        pass


# === MARKET HOURS CHECK ===

def _is_market_hours() -> bool:
    """Check if US stock market is currently open (9:30 AM - 4:00 PM ET, weekdays)."""
    try:
        import pytz
        et = pytz.timezone("US/Eastern")
        now = datetime.now(et)
    except ImportError:
        # Fallback: approximate ET as UTC-5 (ignores DST but close enough)
        from datetime import timezone
        et_offset = timezone(timedelta(hours=-5))
        now = datetime.now(et_offset)

    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


# === PRICE FETCHING ===

async def _get_stock_price(ticker: str) -> float:
    """Get current stock price from Yahoo Finance chart API."""
    try:
        async with httpx.AsyncClient() as client:
            # Use Yahoo Finance chart API (no auth needed)
            resp = await client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("chart", {}).get("result", [])
                if result:
                    meta = result[0].get("meta", {})
                    return float(meta.get("regularMarketPrice", 0))
    except:
        pass
    return 0


# === ENTRY LOGIC ===

async def _check_for_entries():
    config = await get_config()

    async with async_session() as db:
        # Count open positions
        open_count = (await db.execute(
            select(func.count()).select_from(StockPaperPosition).where(StockPaperPosition.status == "OPEN")
        )).scalar() or 0

        if open_count >= config["max_open_positions"]:
            return

        # Get recent stock signals (last 24 hours)
        from .stocks_tracker import StockSignal, StockWhaleMove
        from .sentiment_tracker import SocialMention

        cutoff_24h = datetime.utcnow() - timedelta(hours=24)
        cutoff_7d = datetime.utcnow() - timedelta(days=7)

        signals = (await db.execute(
            select(StockSignal)
            .where(StockSignal.detected_at >= cutoff_24h)
            .order_by(StockSignal.detected_at.desc())
            .limit(20)
        )).scalars().all()

        for signal in signals:
            ticker = (signal.ticker or "").upper()
            if not ticker:
                continue

            # Check if require_buy_direction — skip sale signals
            if config["require_buy_direction"]:
                # cross_source and cluster signals are buy signals by nature
                # but check signal description/type for sell indicators
                desc_lower = (signal.description or "").lower()
                if "sale" in desc_lower or "sell" in desc_lower:
                    continue

            # Check if we already have a position in this ticker
            existing = (await db.execute(
                select(StockPaperPosition)
                .where(StockPaperPosition.ticker == ticker,
                       StockPaperPosition.status == "OPEN")
            )).scalar_one_or_none()
            if existing:
                continue

            # === Score Calculation ===
            score = 0
            sources = []

            # Signal type scoring
            sig_type = (signal.signal_type or "").lower()
            if sig_type == "cross_source":
                score += 30
            elif sig_type == "congress_cluster":
                score += 25
            elif sig_type == "insider_cluster":
                score += 20
            elif sig_type == "13f_convergence":
                score += 15

            # Count insider BUYS in last 7 days
            insider_buys = (await db.execute(
                select(func.count()).select_from(StockWhaleMove)
                .where(StockWhaleMove.ticker == ticker,
                       StockWhaleMove.source == "insider",
                       StockWhaleMove.action == "Purchase",
                       StockWhaleMove.created_at >= cutoff_7d)
            )).scalar() or 0
            if insider_buys > 0:
                score += min(insider_buys * 5, 20)
                sources.append("insider")

            # Count congress BUYS in last 7 days
            congress_buys = (await db.execute(
                select(func.count()).select_from(StockWhaleMove)
                .where(StockWhaleMove.ticker == ticker,
                       StockWhaleMove.source == "congress",
                       StockWhaleMove.action == "Purchase",
                       StockWhaleMove.created_at >= cutoff_7d)
            )).scalar() or 0
            if congress_buys > 0:
                score += min(congress_buys * 8, 24)
                sources.append("congress")

            # 13f source
            has_13f = (await db.execute(
                select(func.count()).select_from(StockWhaleMove)
                .where(StockWhaleMove.ticker == ticker,
                       StockWhaleMove.source == "13f",
                       StockWhaleMove.created_at >= cutoff_7d)
            )).scalar() or 0
            if has_13f > 0:
                sources.append("13f")

            # Total value bonus
            if signal.total_value and float(signal.total_value) > 1000000:
                score += 10

            # Check min_insider_value
            if config["min_insider_value"] > 0 and insider_buys > 0:
                max_value = (await db.execute(
                    select(func.max(StockWhaleMove.value_usd))
                    .where(StockWhaleMove.ticker == ticker,
                           StockWhaleMove.source == "insider",
                           StockWhaleMove.action == "Purchase",
                           StockWhaleMove.created_at >= cutoff_7d)
                )).scalar() or 0
                if float(max_value) < config["min_insider_value"]:
                    # Remove insider from sources if below threshold
                    if "insider" in sources:
                        sources.remove("insider")
                        score -= min(insider_buys * 5, 20)

            # Check social sentiment
            sent_bullish = (await db.execute(
                select(func.count()).select_from(SocialMention)
                .where(SocialMention.token_symbol == ticker,
                       SocialMention.detected_at >= cutoff_24h)
            )).scalar() or 0
            if sent_bullish >= 3:
                score += 10
                sources.append("social")

            # Options flow — check for options mentions in sources field
            if signal.sources and "options" in signal.sources.lower():
                sources.append("options")

            # === Entry Criteria ===
            if score < config["min_agiotage_score"]:
                continue
            if len(sources) < config["min_sources"]:
                continue

            # Get current price
            price = await _get_stock_price(ticker)
            if price <= 0:
                continue

            # Get insider names for alert
            insider_names = []
            if insider_buys > 0 or congress_buys > 0:
                recent_filers = (await db.execute(
                    select(StockWhaleMove.filer_name)
                    .where(StockWhaleMove.ticker == ticker,
                           StockWhaleMove.action == "Purchase",
                           StockWhaleMove.created_at >= cutoff_7d)
                    .limit(5)
                )).scalars().all()
                insider_names = [n for n in recent_filers if n]

            # ENTRY — open paper position
            position = StockPaperPosition(
                ticker=ticker,
                company_name=None,
                entry_price=Decimal(str(price)),
                position_size_usd=Decimal(str(config["position_size_usd"])),
                current_price=Decimal(str(price)),
                highest_price=Decimal(str(price)),
                pnl_pct=Decimal("0"),
                pnl_usd=Decimal("0"),
                entry_signal=sig_type,
                entry_sources=",".join(sources),
                agiotage_score=score,
            )
            db.add(position)

            trade = StockPaperTrade(
                position_id=0,  # Updated after commit
                action="BUY",
                pct_of_position=100,
                price=Decimal(str(price)),
                usd_value=Decimal(str(config["position_size_usd"])),
                pnl_pct=Decimal("0"),
                reason=f"Signal: {sig_type}, Score: {score}, Sources: {','.join(sources)}",
            )

            await db.commit()
            await db.refresh(position)
            trade.position_id = position.id
            db.add(trade)
            await db.commit()

            _log.info(f"PAPER BUY: ${ticker} @ ${price:.2f} Score={score} [{','.join(sources)}]")

            # Telegram alert
            try:
                bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                if bot_token and chat_id:
                    filers_str = ", ".join(insider_names[:3]) if insider_names else "N/A"
                    msg = (
                        f"📈 *PAPER BUY: ${ticker}*\n\n"
                        f"Price: ${price:.2f}\n"
                        f"Size: ${config['position_size_usd']}\n"
                        f"Score: {score}\n"
                        f"Sources: {', '.join(sources)}\n"
                        f"Filers: {filers_str}\n"
                    )
                    async with httpx.AsyncClient() as client:
                        await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown",
                                  "disable_web_page_preview": True}, timeout=5)
            except:
                pass


# === POSITION MANAGEMENT ===

async def _manage_positions():
    config = await get_config()
    max_holding_hours = config["max_holding_days"] * 24

    # Skip price updates outside market hours
    if not _is_market_hours():
        return

    async with async_session() as db:
        positions = (await db.execute(
            select(StockPaperPosition).where(StockPaperPosition.status == "OPEN")
        )).scalars().all()

        for pos in positions:
            price = await _get_stock_price(pos.ticker)
            if price <= 0:
                continue

            entry = float(pos.entry_price)
            pnl_pct = ((price - entry) / entry) * 100
            pnl_usd = (pnl_pct / 100) * float(pos.position_size_usd) * (float(pos.remaining_pct) / 100)
            highest = max(float(pos.highest_price or price), price)

            pos.current_price = Decimal(str(price))
            pos.highest_price = Decimal(str(highest))
            pos.pnl_pct = Decimal(str(round(pnl_pct, 4)))
            pos.pnl_usd = Decimal(str(round(pnl_usd, 2)))
            pos.last_updated = datetime.utcnow()

            remaining = float(pos.remaining_pct)

            # Check take profit levels
            for tp in config["take_profit_levels"]:
                if pnl_pct >= tp["at_profit_pct"] and remaining > 0:
                    sell_pct = min(tp["sell_pct"], remaining)
                    if sell_pct <= 0:
                        continue

                    # Check if we already took this TP level
                    existing_tp = (await db.execute(
                        select(StockPaperTrade)
                        .where(StockPaperTrade.position_id == pos.id,
                               StockPaperTrade.reason.contains(f"TP {tp['at_profit_pct']}%"))
                    )).scalar_one_or_none()
                    if existing_tp:
                        continue

                    usd_val = float(pos.position_size_usd) * (sell_pct / 100) * (1 + pnl_pct / 100)
                    trade = StockPaperTrade(
                        position_id=pos.id, action="SELL",
                        pct_of_position=sell_pct, price=Decimal(str(price)),
                        usd_value=Decimal(str(round(usd_val, 2))),
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        reason=f"TP {tp['at_profit_pct']}% hit — sold {sell_pct}%",
                    )
                    db.add(trade)
                    pos.remaining_pct = Decimal(str(remaining - sell_pct))
                    remaining -= sell_pct

                    _log.info(f"PAPER SELL (TP): ${pos.ticker} {sell_pct}% @ +{pnl_pct:.1f}%")

                    # Telegram
                    try:
                        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                        if bot_token and chat_id:
                            msg = f"💰 *PAPER SELL: ${pos.ticker}*\nTP {tp['at_profit_pct']}% hit\nSold {sell_pct}% @ +{pnl_pct:.1f}%\nValue: ${usd_val:.2f}"
                            async with httpx.AsyncClient() as client:
                                await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=5)
                    except:
                        pass

            # Check stop loss
            if pnl_pct <= -config["stop_loss_pct"] and remaining > 0:
                usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                trade = StockPaperTrade(
                    position_id=pos.id, action="SELL",
                    pct_of_position=remaining, price=Decimal(str(price)),
                    usd_value=Decimal(str(round(usd_val, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=f"Stop loss at {pnl_pct:.1f}%",
                )
                db.add(trade)
                pos.remaining_pct = Decimal("0")
                pos.status = "CLOSED"
                pos.close_reason = f"Stop loss ({pnl_pct:.1f}%)"
                pos.closed_at = datetime.utcnow()

                _log.info(f"PAPER SELL (SL): ${pos.ticker} 100% @ {pnl_pct:.1f}%")
                try:
                    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                    if bot_token and chat_id:
                        msg = f"🔴 *PAPER STOP LOSS: ${pos.ticker}*\nSold 100% @ {pnl_pct:.1f}%\nLoss: ${abs(usd_val):.2f}"
                        async with httpx.AsyncClient() as client:
                            await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=5)
                except:
                    pass

            # Check trailing stop
            if config["trailing_stop_enabled"] and remaining > 0:
                highest_pnl = ((highest - entry) / entry) * 100
                if highest_pnl >= config["trailing_stop_activation_pct"]:
                    trail_from = highest * (1 - config["trailing_stop_trail_pct"] / 100)
                    if price <= trail_from:
                        usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                        trade = StockPaperTrade(
                            position_id=pos.id, action="SELL",
                            pct_of_position=remaining, price=Decimal(str(price)),
                            usd_value=Decimal(str(round(usd_val, 2))),
                            pnl_pct=Decimal(str(round(pnl_pct, 4))),
                            reason=f"Trailing stop (peak +{highest_pnl:.1f}%, trailed to +{pnl_pct:.1f}%)",
                        )
                        db.add(trade)
                        pos.remaining_pct = Decimal("0")
                        pos.status = "CLOSED"
                        pos.close_reason = f"Trailing stop (+{pnl_pct:.1f}% from +{highest_pnl:.1f}% peak)"
                        pos.closed_at = datetime.utcnow()

                        _log.info(f"PAPER SELL (TRAIL): ${pos.ticker} @ +{pnl_pct:.1f}% (peak +{highest_pnl:.1f}%)")
                        try:
                            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                            if bot_token and chat_id:
                                msg = f"📉 *PAPER TRAILING STOP: ${pos.ticker}*\nPeak: +{highest_pnl:.1f}%\nSold @ +{pnl_pct:.1f}%\nValue: ${usd_val:.2f}"
                                async with httpx.AsyncClient() as client:
                                    await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                        json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=5)
                        except:
                            pass

            # Check max holding time
            age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
            if age_hours >= max_holding_hours and remaining > 0:
                usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                trade = StockPaperTrade(
                    position_id=pos.id, action="SELL",
                    pct_of_position=remaining, price=Decimal(str(price)),
                    usd_value=Decimal(str(round(usd_val, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=f"Max hold time ({config['max_holding_days']}d) expired @ {pnl_pct:.1f}%",
                )
                db.add(trade)
                pos.remaining_pct = Decimal("0")
                pos.status = "CLOSED"
                pos.close_reason = f"Max hold time ({pnl_pct:.1f}%)"
                pos.closed_at = datetime.utcnow()

            # Close position if fully sold
            if float(pos.remaining_pct) <= 0 and pos.status == "OPEN":
                pos.status = "CLOSED"
                pos.closed_at = datetime.utcnow()
                if not pos.close_reason:
                    pos.close_reason = "Fully sold via take profits"

            await asyncio.sleep(0.5)

        await db.commit()


# === MAIN LOOP ===

async def run():
    _log.info("Stock Paper Trading Bot starting")
    await asyncio.sleep(70)

    config = await get_config()
    _log.info(f"Config: score>={config['min_agiotage_score']}, "
              f"size=${config['position_size_usd']}, max={config['max_open_positions']} positions, "
              f"SL={config['stop_loss_pct']}%, trail={config['trailing_stop_trail_pct']}%, "
              f"max_hold={config['max_holding_days']}d")

    last_entry_check = 0

    while True:
        try:
            now = time.time()

            # Entry logic every 30 minutes
            if now - last_entry_check >= ENTRY_CHECK_INTERVAL:
                await _check_for_entries()
                last_entry_check = now

            # Position management every 5 minutes (only during market hours)
            await _manage_positions()

        except Exception as e:
            _log.error(f"Stock paper trader error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

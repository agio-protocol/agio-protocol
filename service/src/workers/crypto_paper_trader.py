# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Crypto Paper Trading Bot — simulates trades on large-cap crypto using whale flow
and correlated signals. Uses CoinGecko for price tracking.
Fully configurable: entry criteria, take profit levels, stop losses, trailing stops.
All parameters adjustable via API without redeploying.
"""
import asyncio
import logging
import os
import json as _json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Float, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("crypto-paper-trader")

POLL_INTERVAL = 60


# === DEFAULT CONFIG (adjustable via Redis) ===
DEFAULT_CONFIG = {
    "min_confidence": 50,
    "min_sources": 1,
    "position_size_usd": 500,
    "max_open_positions": 5,
    "max_holding_hours": 48,
    "take_profit_levels": [
        {"sell_pct": 33, "at_profit_pct": 5},
        {"sell_pct": 33, "at_profit_pct": 10},
        {"sell_pct": 100, "at_profit_pct": 20},
    ],
    "stop_loss_pct": 8,
    "trailing_stop_enabled": True,
    "trailing_stop_activation_pct": 10,
    "trailing_stop_trail_pct": 5,
    "allowed_symbols": [
        "BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "ADA", "DOT", "MATIC",
        "NEAR", "ARB", "OP", "SUI", "APT", "INJ", "TIA", "SEI", "JUP",
        "WIF", "PEPE", "RENDER", "FET", "TAO", "ONDO", "AAVE",
    ],
    "skip_symbols": ["USDC", "USDT", "DAI", "BUSD"],
}


# === DB MODELS ===

class CryptoPaperPosition(Base):
    __tablename__ = "crypto_paper_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    side: Mapped[str] = mapped_column(String(10), default="long")
    entry_price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    position_size_usd: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    remaining_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    current_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    entry_signal: Mapped[str | None] = mapped_column(String(30), nullable=True)
    entry_sources: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_cpp_status", "status"),
        Index("idx_cpp_opened", "opened_at"),
        Index("idx_cpp_ticker", "ticker"),
    )


class CryptoPaperTrade(Base):
    __tablename__ = "crypto_paper_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    pct_of_position: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    usd_value: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_cpt_position", "position_id"),
    )


# === CONFIG MANAGEMENT ===

async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("crypto_paper_trader_config")
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
        await redis_client.set("crypto_paper_trader_config", _json.dumps(current))
    except:
        pass


# === PRICE FETCHING ===

CG_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "AVAX": "avalanche-2",
    "LINK": "chainlink", "DOGE": "dogecoin", "ADA": "cardano", "DOT": "polkadot",
    "MATIC": "matic-network", "NEAR": "near", "ARB": "arbitrum", "OP": "optimism",
    "SUI": "sui", "APT": "aptos", "INJ": "injective-protocol", "TIA": "celestia",
    "SEI": "sei-network", "JUP": "jupiter-exchange-solana", "WIF": "dogwifcoin",
    "PEPE": "pepe", "RENDER": "render-token", "FET": "fetch-ai", "TAO": "bittensor",
    "ONDO": "ondo-finance", "AAVE": "aave",
}


async def _get_crypto_price(symbol: str) -> float:
    try:
        cg_id = CG_IDS.get(symbol.upper())
        if not cg_id:
            return 0
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd",
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                return float(data.get(cg_id, {}).get("usd", 0))
    except:
        pass
    return 0


# === ENTRY LOGIC ===

async def _check_for_entries():
    config = await get_config()

    async with async_session() as db:
        # Count open positions
        open_count = (await db.execute(
            select(func.count()).select_from(CryptoPaperPosition)
            .where(CryptoPaperPosition.status == "OPEN")
        )).scalar() or 0

        if open_count >= config["max_open_positions"]:
            return

        # Get recent whale signals (CryptoSignal) — BUY direction, STRONG or VERY_STRONG
        from .whale_tracker import CryptoSignal
        cutoff = datetime.utcnow() - timedelta(minutes=60)

        whale_signals = (await db.execute(
            select(CryptoSignal)
            .where(
                CryptoSignal.detected_at >= cutoff,
                CryptoSignal.direction == "BUY",
                CryptoSignal.strength.in_(["STRONG", "VERY_STRONG"]),
            )
            .order_by(CryptoSignal.detected_at.desc())
            .limit(20)
        )).scalars().all()

        # Get recent correlated signals
        from .correlation_engine import CorrelatedSignal
        correlated_signals = (await db.execute(
            select(CorrelatedSignal)
            .where(
                CorrelatedSignal.detected_at >= cutoff,
                CorrelatedSignal.confidence >= 50,
            )
            .order_by(CorrelatedSignal.detected_at.desc())
            .limit(20)
        )).scalars().all()

        # Get recent momentum signals (volume spikes, breakouts)
        from .momentum_scanner import MomentumSignal
        momentum_signals = (await db.execute(
            select(MomentumSignal)
            .where(
                MomentumSignal.detected_at >= cutoff,
                MomentumSignal.strength.in_(["STRONG", "VERY_STRONG"]),
                MomentumSignal.signal_type.in_(["volume_spike", "momentum_up", "breakout"]),
            )
            .order_by(MomentumSignal.detected_at.desc())
            .limit(20)
        )).scalars().all()

        # Build a map of symbols with their signals
        _empty = lambda: {"whale_signals": [], "correlated": [], "momentum": [], "score": 0, "sources": []}
        symbol_data = {}

        for sig in whale_signals:
            sym = (sig.symbol or "").upper()
            if sym not in config["allowed_symbols"] or sym in config["skip_symbols"]:
                continue
            if sym not in symbol_data:
                symbol_data[sym] = _empty()
            symbol_data[sym]["whale_signals"].append(sig)

        for sig in correlated_signals:
            sym = (sig.token_symbol or "").upper()
            if sym not in config["allowed_symbols"] or sym in config["skip_symbols"]:
                continue
            if sym not in symbol_data:
                symbol_data[sym] = _empty()
            symbol_data[sym]["correlated"].append(sig)

        for sig in momentum_signals:
            sym = (sig.symbol or "").upper()
            if sym not in config["allowed_symbols"] or sym in config["skip_symbols"]:
                continue
            if sym not in symbol_data:
                symbol_data[sym] = _empty()
            symbol_data[sym]["momentum"].append(sig)

        for symbol, data in symbol_data.items():
            # Check if we already have a position in this symbol
            existing = (await db.execute(
                select(CryptoPaperPosition)
                .where(CryptoPaperPosition.ticker == symbol,
                       CryptoPaperPosition.status == "OPEN")
            )).scalar_one_or_none()
            if existing:
                continue

            # Calculate score
            score = 0
            sources = []

            # Whale flow scoring
            if data["whale_signals"]:
                sources.append("whale_flow")
                best_whale = data["whale_signals"][0]
                if best_whale.strength == "VERY_STRONG":
                    score += 30
                elif best_whale.strength == "STRONG":
                    score += 20
                # Whale volume > $10M adds +10
                if best_whale.total_usd and float(best_whale.total_usd) > 10_000_000:
                    score += 10

            # Correlated signal scoring
            if data["correlated"]:
                sources.append("correlated")
                best_corr = max(data["correlated"], key=lambda s: s.confidence)
                score += best_corr.confidence // 2

            # Momentum signal scoring
            if data["momentum"]:
                sources.append("momentum")
                best_mom = data["momentum"][0]
                if best_mom.strength == "VERY_STRONG":
                    score += 25
                elif best_mom.strength == "STRONG":
                    score += 15
                if best_mom.signal_type == "breakout":
                    score += 10
                if best_mom.volume_ratio and float(best_mom.volume_ratio) >= 5:
                    score += 5

            # Check for social mentions (sentiment)
            try:
                from .sentiment_tracker import SocialMention
                sent_count = (await db.execute(
                    select(func.count()).select_from(SocialMention)
                    .where(SocialMention.token_symbol == symbol,
                           SocialMention.detected_at >= datetime.utcnow() - timedelta(hours=6))
                )).scalar() or 0
                if sent_count >= 5:
                    score += 5
                    sources.append("sentiment")
            except:
                pass

            # Check min requirements
            if score < config["min_confidence"]:
                continue
            if len(sources) < config["min_sources"]:
                continue

            # Check max positions again (may have opened one this loop)
            open_count = (await db.execute(
                select(func.count()).select_from(CryptoPaperPosition)
                .where(CryptoPaperPosition.status == "OPEN")
            )).scalar() or 0
            if open_count >= config["max_open_positions"]:
                return

            # Get current price
            price = await _get_crypto_price(symbol)
            if price <= 0:
                continue

            # ENTRY — open paper position
            signal_str = data["whale_signals"][0].strength if data["whale_signals"] else "CORRELATED"
            position = CryptoPaperPosition(
                ticker=symbol,
                token_symbol=symbol,
                entry_price=Decimal(str(price)),
                position_size_usd=Decimal(str(config["position_size_usd"])),
                current_price=Decimal(str(price)),
                highest_price=Decimal(str(price)),
                pnl_pct=Decimal("0"),
                pnl_usd=Decimal("0"),
                entry_signal=signal_str,
                entry_sources=",".join(sources),
                score=score,
            )
            db.add(position)

            trade = CryptoPaperTrade(
                position_id=0,
                action="BUY",
                pct_of_position=100,
                price=Decimal(str(price)),
                usd_value=Decimal(str(config["position_size_usd"])),
                pnl_pct=Decimal("0"),
                reason=f"Signal: {signal_str}, Score: {score}, Sources: {','.join(sources)}",
            )

            await db.commit()
            await db.refresh(position)
            trade.position_id = position.id
            db.add(trade)
            await db.commit()

            _log.info(f"PAPER BUY: ${symbol} @ ${price:,.2f} Score={score} [{','.join(sources)}]")

            # Telegram alert
            try:
                bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                if bot_token and chat_id:
                    msg = (
                        f"\U0001f4ca *PAPER BUY: ${symbol}*\n\n"
                        f"Price: ${price:,.2f}\n"
                        f"Size: ${config['position_size_usd']}\n"
                        f"Score: {score}\n"
                        f"Sources: {', '.join(sources)}\n"
                    )
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown",
                                  "disable_web_page_preview": True},
                            timeout=5,
                        )
            except:
                pass


# === POSITION MANAGEMENT ===

async def _manage_positions():
    config = await get_config()

    async with async_session() as db:
        positions = (await db.execute(
            select(CryptoPaperPosition).where(CryptoPaperPosition.status == "OPEN")
        )).scalars().all()

        for pos in positions:
            price = await _get_crypto_price(pos.ticker)
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
                        select(CryptoPaperTrade)
                        .where(CryptoPaperTrade.position_id == pos.id,
                               CryptoPaperTrade.reason.contains(f"TP {tp['at_profit_pct']}%"))
                    )).scalar_one_or_none()
                    if existing_tp:
                        continue

                    usd_val = float(pos.position_size_usd) * (sell_pct / 100) * (1 + pnl_pct / 100)
                    trade = CryptoPaperTrade(
                        position_id=pos.id, action="SELL",
                        pct_of_position=sell_pct, price=Decimal(str(price)),
                        usd_value=Decimal(str(round(usd_val, 2))),
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        reason=f"TP {tp['at_profit_pct']}% hit \u2014 sold {sell_pct}%",
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
                            msg = f"\U0001f4b0 *PAPER SELL: ${pos.ticker}*\nTP {tp['at_profit_pct']}% hit\nSold {sell_pct}% @ +{pnl_pct:.1f}%\nValue: ${usd_val:.2f}"
                            async with httpx.AsyncClient() as client:
                                await client.post(
                                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                                    timeout=5,
                                )
                    except:
                        pass

            # Check stop loss
            if pnl_pct <= -config["stop_loss_pct"] and remaining > 0:
                usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                trade = CryptoPaperTrade(
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
                        msg = f"\U0001f534 *PAPER STOP LOSS: ${pos.ticker}*\nSold 100% @ {pnl_pct:.1f}%\nLoss: ${abs(usd_val):.2f}"
                        async with httpx.AsyncClient() as client:
                            await client.post(
                                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                                timeout=5,
                            )
                except:
                    pass

            # Check trailing stop
            if config["trailing_stop_enabled"] and remaining > 0:
                highest_pnl = ((highest - entry) / entry) * 100
                if highest_pnl >= config["trailing_stop_activation_pct"]:
                    trail_from = highest * (1 - config["trailing_stop_trail_pct"] / 100)
                    if price <= trail_from:
                        usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                        trade = CryptoPaperTrade(
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
                                msg = f"\U0001f4c9 *PAPER TRAILING STOP: ${pos.ticker}*\nPeak: +{highest_pnl:.1f}%\nSold @ +{pnl_pct:.1f}%\nValue: ${usd_val:.2f}"
                                async with httpx.AsyncClient() as client:
                                    await client.post(
                                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                        json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                                        timeout=5,
                                    )
                        except:
                            pass

            # Check max holding time
            age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
            if age_hours >= config["max_holding_hours"] and remaining > 0:
                usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                trade = CryptoPaperTrade(
                    position_id=pos.id, action="SELL",
                    pct_of_position=remaining, price=Decimal(str(price)),
                    usd_value=Decimal(str(round(usd_val, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=f"Max hold time ({config['max_holding_hours']}h) expired @ {pnl_pct:.1f}%",
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
    _log.info("Crypto Paper Trading Bot starting")
    await asyncio.sleep(65)

    config = await get_config()
    _log.info(
        f"Config: confidence>={config['min_confidence']}, "
        f"size=${config['position_size_usd']}, max={config['max_open_positions']} positions, "
        f"SL={config['stop_loss_pct']}%, trail={config['trailing_stop_trail_pct']}%"
    )

    while True:
        try:
            await _check_for_entries()
            await _manage_positions()
        except Exception as e:
            _log.error(f"Crypto paper trader error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

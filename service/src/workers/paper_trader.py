# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Paper Trading Bot — simulates trades using Agiotage signals.
Fully configurable: entry criteria, take profit levels, stop losses, trailing stops.
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

_log = logging.getLogger("paper-trader")

POLL_INTERVAL = 30


# === DEFAULT CONFIG (adjustable via Redis) ===
DEFAULT_CONFIG = {
    # Entry criteria
    "min_agiotage_score": 40,
    "min_mc": 100000,
    "min_sources": 2,
    "min_wallet_count": 3,

    # Position sizing
    "position_size_usd": 100,
    "max_open_positions": 10,
    "max_holding_hours": 24,

    # Take profit levels: sell X% of position at Y% profit
    "take_profit_levels": [
        {"sell_pct": 50, "at_profit_pct": 50},
        {"sell_pct": 25, "at_profit_pct": 75},
        {"sell_pct": 100, "at_profit_pct": 200},
    ],

    # Stop loss
    "stop_loss_pct": 30,

    # Trailing stop
    "trailing_stop_enabled": True,
    "trailing_stop_activation_pct": 30,
    "trailing_stop_trail_pct": 15,

    # Filters
    "require_security_check": True,
    "max_rug_ratio": 0.3,
    "require_renounced_mint": False,
    "min_liquidity": 50000,
    "skip_symbols": ["WSOL", "WETH", "WBTC", "USDC", "USDT", "SOL"],
}


# === DB MODELS ===

class PaperPosition(Base):
    __tablename__ = "paper_positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    side: Mapped[str] = mapped_column(String(10), default="long")
    entry_price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    entry_mc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    position_size_usd: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    remaining_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    current_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    current_mc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    highest_price: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
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
        Index("idx_pp_status", "status"),
        Index("idx_pp_opened", "opened_at"),
    )


class PaperTrade(Base):
    __tablename__ = "paper_trades"
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
        Index("idx_pt_position", "position_id"),
    )


# === CONFIG MANAGEMENT ===

async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("paper_trader_config")
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
        await redis_client.set("paper_trader_config", _json.dumps(current))
    except:
        pass


# === PRICE FETCHING ===

async def _get_price_mc(token_addr: str) -> tuple:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{token_addr}", timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if pairs:
                    return float(pairs[0].get("priceUsd", 0) or 0), float(pairs[0].get("fdv", 0) or 0)
    except:
        pass
    return 0, 0


# === ENTRY LOGIC ===

async def _check_for_entries():
    config = await get_config()

    async with async_session() as db:
        # Count open positions
        open_count = (await db.execute(
            select(func.count()).select_from(PaperPosition).where(PaperPosition.status == "OPEN")
        )).scalar() or 0

        if open_count >= config["max_open_positions"]:
            return

        # Get recent cluster signals
        from .smart_money_tracker import ClusterSignal
        cutoff = datetime.utcnow() - timedelta(minutes=15)

        signals = (await db.execute(
            select(ClusterSignal)
            .where(ClusterSignal.detected_at >= cutoff,
                   ClusterSignal.wallet_count >= config["min_wallet_count"])
            .order_by(ClusterSignal.detected_at.desc())
            .limit(10)
        )).scalars().all()

        for signal in signals:
            symbol = (signal.token_symbol or "").upper()
            if symbol in config["skip_symbols"]:
                continue

            # Check if we already have a position in this token
            existing = (await db.execute(
                select(PaperPosition)
                .where(PaperPosition.token_address == signal.token_address,
                       PaperPosition.status == "OPEN")
            )).scalar_one_or_none()
            if existing:
                continue

            # Get current price and MC
            price, mc = await _get_price_mc(signal.token_address)
            if price <= 0 or mc < config["min_mc"]:
                continue

            # Check liquidity
            if mc < config["min_liquidity"]:
                continue

            # Calculate Agiotage Score for this token
            score = 0
            sources = []
            cl_boost = {"VERY_STRONG": 25, "STRONG": 20, "MEDIUM": 15}.get(signal.signal_strength, 10)
            score += cl_boost
            sources.append("smart_money")

            if signal.avg_wallet_winrate and float(signal.avg_wallet_winrate) > 0.5:
                score += 10
            if signal.is_deployer_token:
                score += 15
                sources.append("deployer")

            if score < config["min_agiotage_score"]:
                continue
            if len(sources) < config["min_sources"]:
                # Check if sentiment also agrees
                from .sentiment_tracker import SocialMention
                sent_count = (await db.execute(
                    select(func.count()).select_from(SocialMention)
                    .where(SocialMention.token_symbol == symbol,
                           SocialMention.detected_at >= datetime.utcnow() - timedelta(hours=6))
                )).scalar() or 0
                if sent_count >= 5:
                    score += 10
                    sources.append("social")

                if len(sources) < config["min_sources"]:
                    continue

            # Check if live mode is enabled
            live_mode = False
            try:
                from ..core.redis import redis_client
                tc = await redis_client.get("trading:config")
                if tc:
                    tc_data = _json.loads(tc)
                    live_mode = tc_data.get("live_mode", False) and "paper_trader" in tc_data.get("allowed_traders", [])
                    paused = await redis_client.get("trading:paused")
                    if paused == "1":
                        live_mode = False
            except Exception:
                pass

            # Execute live buy if enabled
            live_tx = None
            actual_size = config["position_size_usd"]
            if live_mode:
                try:
                    from ..services.jupiter_swap import buy_token
                    sol_price_resp = await httpx.AsyncClient().get(
                        "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=5)
                    sol_price = sol_price_resp.json().get("solana", {}).get("usd", 150) if sol_price_resp.status_code == 200 else 150
                    amount_sol = actual_size / sol_price
                    live_tx = await buy_token(signal.token_address, amount_sol, slippage_bps=200)
                    if not live_tx.get("success"):
                        _log.error(f"LIVE BUY FAILED: ${symbol} — {live_tx.get('error')}")
                        live_mode = False
                    else:
                        _log.info(f"LIVE BUY: ${symbol} tx={live_tx.get('tx_hash')}")
                except Exception as e:
                    _log.error(f"LIVE BUY ERROR: ${symbol} — {e}")
                    live_mode = False

            # Record position
            position = PaperPosition(
                token_address=signal.token_address,
                token_symbol=symbol,
                entry_price=Decimal(str(price)),
                entry_mc=Decimal(str(mc)),
                position_size_usd=Decimal(str(actual_size)),
                current_price=Decimal(str(price)),
                current_mc=Decimal(str(mc)),
                highest_price=Decimal(str(price)),
                pnl_pct=Decimal("0"),
                pnl_usd=Decimal("0"),
                entry_signal=signal.signal_strength,
                entry_sources=",".join(sources),
                agiotage_score=score,
            )
            db.add(position)

            reason = f"Signal: {signal.signal_strength}, {signal.wallet_count} wallets, Score: {score}"
            if live_tx and live_tx.get("success"):
                reason += f" [LIVE tx:{live_tx['tx_hash'][:12]}]"

            trade = PaperTrade(
                position_id=0,
                action="BUY",
                pct_of_position=100,
                price=Decimal(str(price)),
                usd_value=Decimal(str(actual_size)),
                pnl_pct=Decimal("0"),
                reason=reason,
            )

            await db.commit()
            await db.refresh(position)
            trade.position_id = position.id
            db.add(trade)
            await db.commit()

            mode_label = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
            _log.info(f"{mode_label} BUY: ${symbol} @ ${price:.10f} MC=${mc:,.0f} Score={score} [{','.join(sources)}]")

            # Telegram alert
            try:
                bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                if bot_token and chat_id:
                    ca = signal.token_address
                    tx_line = f"\n✅ TX: `{live_tx['tx_hash'][:20]}...`" if live_tx and live_tx.get("success") else ""
                    msg = (
                        f"{'💰' if mode_label == 'LIVE' else '📝'} *{mode_label} BUY: ${symbol}*\n\n"
                        f"Price: ${price:.8f}\n"
                        f"MC: ${mc:,.0f}\n"
                        f"Size: ${actual_size}\n"
                        f"Score: {score}\n"
                        f"Sources: {', '.join(sources)}{tx_line}\n\n"
                        f"CA: `{ca}`\n"
                        f"[Chart](https://dexscreener.com/solana/{ca})"
                    )
                    async with httpx.AsyncClient() as client:
                        await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown",
                                  "disable_web_page_preview": True}, timeout=5)
            except:
                pass


async def _is_live_mode() -> bool:
    try:
        from ..core.redis import redis_client
        paused = await redis_client.get("trading:paused")
        if paused == "1":
            return False
        tc = await redis_client.get("trading:config")
        if tc:
            tc_data = _json.loads(tc)
            return tc_data.get("live_mode", False) and "paper_trader" in tc_data.get("allowed_traders", [])
    except Exception:
        pass
    return False


async def _live_sell(token_address: str, sell_pct: float, reason: str) -> dict | None:
    """Execute a live sell if live mode is on. Returns tx result or None."""
    if not await _is_live_mode():
        return None
    try:
        from ..services.jupiter_swap import sell_token, get_token_balance
        raw_balance, ui_balance, decimals = await get_token_balance(token_address)
        if raw_balance <= 0:
            return None
        sell_amount = int(raw_balance * (sell_pct / 100))
        if sell_amount <= 0:
            return None
        result = await sell_token(token_address, sell_amount, decimals, slippage_bps=300)
        if result.get("success"):
            _log.info(f"LIVE SELL: {reason} tx={result.get('tx_hash')}")
        else:
            _log.error(f"LIVE SELL FAILED: {reason} — {result.get('error')}")
        return result
    except Exception as e:
        _log.error(f"LIVE SELL ERROR: {e}")
        return None


# === POSITION MANAGEMENT ===

async def _manage_positions():
    config = await get_config()

    async with async_session() as db:
        positions = (await db.execute(
            select(PaperPosition).where(PaperPosition.status == "OPEN")
        )).scalars().all()

        for pos in positions:
            price, mc = await _get_price_mc(pos.token_address)
            if price <= 0:
                continue

            entry = float(pos.entry_price)
            pnl_pct = ((price - entry) / entry) * 100
            pnl_usd = (pnl_pct / 100) * float(pos.position_size_usd) * (float(pos.remaining_pct) / 100)
            highest = max(float(pos.highest_price or price), price)

            pos.current_price = Decimal(str(price))
            pos.current_mc = Decimal(str(mc))
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
                        select(PaperTrade)
                        .where(PaperTrade.position_id == pos.id,
                               PaperTrade.reason.contains(f"TP {tp['at_profit_pct']}%"))
                    )).scalar_one_or_none()
                    if existing_tp:
                        continue

                    # Live sell if enabled
                    live_tx = await _live_sell(pos.token_address, sell_pct, f"TP {tp['at_profit_pct']}% ${pos.token_symbol}")
                    tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]" if live_tx and live_tx.get("success") else ""

                    usd_val = float(pos.position_size_usd) * (sell_pct / 100) * (1 + pnl_pct / 100)
                    trade = PaperTrade(
                        position_id=pos.id, action="SELL",
                        pct_of_position=sell_pct, price=Decimal(str(price)),
                        usd_value=Decimal(str(round(usd_val, 2))),
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        reason=f"TP {tp['at_profit_pct']}% hit — sold {sell_pct}%{tx_tag}",
                    )
                    db.add(trade)
                    pos.remaining_pct = Decimal(str(remaining - sell_pct))
                    remaining -= sell_pct

                    mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                    _log.info(f"{mode} SELL (TP): ${pos.token_symbol} {sell_pct}% @ +{pnl_pct:.1f}%")

                    # Telegram
                    try:
                        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                        if bot_token and chat_id:
                            msg = f"💰 *PAPER SELL: ${pos.token_symbol}*\nTP {tp['at_profit_pct']}% hit\nSold {sell_pct}% @ +{pnl_pct:.1f}%\nValue: ${usd_val:.2f}"
                            async with httpx.AsyncClient() as client:
                                await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=5)
                    except:
                        pass

            # Check stop loss
            if pnl_pct <= -config["stop_loss_pct"] and remaining > 0:
                live_tx = await _live_sell(pos.token_address, 100, f"SL ${pos.token_symbol}")
                tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]" if live_tx and live_tx.get("success") else ""

                usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                trade = PaperTrade(
                    position_id=pos.id, action="SELL",
                    pct_of_position=remaining, price=Decimal(str(price)),
                    usd_value=Decimal(str(round(usd_val, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=f"Stop loss at {pnl_pct:.1f}%{tx_tag}",
                )
                db.add(trade)
                pos.remaining_pct = Decimal("0")
                pos.status = "CLOSED"
                pos.close_reason = f"Stop loss ({pnl_pct:.1f}%)"
                pos.closed_at = datetime.utcnow()

                mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                _log.info(f"{mode} SELL (SL): ${pos.token_symbol} 100% @ {pnl_pct:.1f}%")
                try:
                    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                    if bot_token and chat_id:
                        msg = f"🔴 *PAPER STOP LOSS: ${pos.token_symbol}*\nSold 100% @ {pnl_pct:.1f}%\nLoss: ${abs(usd_val):.2f}"
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
                        live_tx = await _live_sell(pos.token_address, 100, f"TRAIL ${pos.token_symbol}")
                        tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]" if live_tx and live_tx.get("success") else ""

                        usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                        trade = PaperTrade(
                            position_id=pos.id, action="SELL",
                            pct_of_position=remaining, price=Decimal(str(price)),
                            usd_value=Decimal(str(round(usd_val, 2))),
                            pnl_pct=Decimal(str(round(pnl_pct, 4))),
                            reason=f"Trailing stop (peak +{highest_pnl:.1f}%, trailed to +{pnl_pct:.1f}%){tx_tag}",
                        )
                        db.add(trade)
                        pos.remaining_pct = Decimal("0")
                        pos.status = "CLOSED"
                        pos.close_reason = f"Trailing stop (+{pnl_pct:.1f}% from +{highest_pnl:.1f}% peak)"
                        pos.closed_at = datetime.utcnow()

                        mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                        _log.info(f"{mode} SELL (TRAIL): ${pos.token_symbol} @ +{pnl_pct:.1f}% (peak +{highest_pnl:.1f}%)")
                        try:
                            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                            if bot_token and chat_id:
                                msg = f"📉 *PAPER TRAILING STOP: ${pos.token_symbol}*\nPeak: +{highest_pnl:.1f}%\nSold @ +{pnl_pct:.1f}%\nValue: ${usd_val:.2f}"
                                async with httpx.AsyncClient() as client:
                                    await client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                        json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=5)
                        except:
                            pass

            # Check max holding time
            age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
            if age_hours >= config["max_holding_hours"] and remaining > 0:
                usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                trade = PaperTrade(
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
    _log.info("Paper Trading Bot starting")
    await asyncio.sleep(55)

    config = await get_config()
    _log.info(f"Config: score>={config['min_agiotage_score']}, MC>=${config['min_mc']:,}, "
              f"size=${config['position_size_usd']}, max={config['max_open_positions']} positions, "
              f"SL={config['stop_loss_pct']}%, trail={config['trailing_stop_trail_pct']}%")

    while True:
        try:
            await _check_for_entries()
            await _manage_positions()
        except Exception as e:
            _log.error(f"Paper trader error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

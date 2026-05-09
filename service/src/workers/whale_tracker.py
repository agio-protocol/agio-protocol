# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Crypto Whale Tracker — monitors large transfers via Whale Alert API."""
import asyncio
import logging
import os
import time
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("whale-tracker")

WHALE_ALERT_KEY = os.getenv("WHALE_ALERT_KEY", "")
WHALE_ALERT_URL = "https://api.whale-alert.io/v1/transactions"
POLL_INTERVAL = 60
MIN_VALUE_USD = 500_000

CEX_NAMES = {"binance", "coinbase", "kraken", "bitfinex", "huobi", "okex", "kucoin",
             "gemini", "bybit", "crypto.com", "ftx", "bitstamp", "gate.io", "mexc"}

STABLECOINS = {"usdc", "usdt", "dai", "busd", "tusd", "gusd", "pax", "husd", "susd", "eurt", "usd1"}

TRACKED_SYMBOLS = {"btc", "eth", "usdt", "usdc", "sol", "xrp", "link", "avax", "matic",
                   "dot", "ada", "atom", "uni", "aave", "mkr", "snx", "dai", "wbtc",
                   "bnb", "shib", "doge", "zec", "pepe", "tao", "sui", "apt", "arb",
                   "op", "sei", "inj", "fet", "rndr", "near", "ftm", "grt", "sand",
                   "mana", "axs", "crv", "ldo", "rpl", "pendle", "jup", "wif",
                   "bonk", "floki", "ondo", "jasmy", "kas", "hbar", "algo",
                   "vet", "icp", "fil", "theta", "egld", "qnt", "chz", "enj"}


class WhaleTransaction(Base):
    __tablename__ = "whale_transactions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    blockchain: Mapped[str] = mapped_column(String(30), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(24, 4), nullable=False)
    amount_usd: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    from_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    from_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    to_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    to_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tx_type: Mapped[str] = mapped_column(String(30), nullable=False)
    trade_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_whale_time", "trade_time"),
        Index("idx_whale_symbol", "symbol"),
        Index("idx_whale_type", "tx_type"),
    )


def _classify_tx(from_owner: str, to_owner: str) -> str:
    """Classify transaction type based on from/to owners."""
    from_is_cex = from_owner.lower() in CEX_NAMES if from_owner else False
    to_is_cex = to_owner.lower() in CEX_NAMES if to_owner else False

    if from_is_cex and not to_is_cex:
        return "exchange_withdrawal"
    elif not from_is_cex and to_is_cex:
        return "exchange_deposit"
    elif from_is_cex and to_is_cex:
        return "exchange_transfer"
    else:
        return "whale_transfer"


LOW_THRESHOLD_MIN = 50_000
LOW_THRESHOLD_COINS = ["zec", "doge", "pepe", "tao", "sui", "apt", "sei", "inj", "fet",
                       "rndr", "wif", "bonk", "floki", "ondo", "jasmy", "kas", "pendle",
                       "jup", "shib", "chz", "enj", "sand", "mana", "axs"]


async def _poll_whale_alert():
    """Fetch whale transactions. $500K+ for majors, $50K+ for smaller caps."""
    if not WHALE_ALERT_KEY:
        return

    start = int(time.time()) - 3600
    all_transactions = []

    async with httpx.AsyncClient() as client:
        # Main poll — $500K+ for all coins
        try:
            resp = await client.get(WHALE_ALERT_URL, params={
                "api_key": WHALE_ALERT_KEY,
                "min_value": MIN_VALUE_USD,
                "start": start,
                "limit": 100,
            }, timeout=15)
            if resp.status_code == 429:
                _log.warning("Whale Alert rate limited")
                await asyncio.sleep(60)
                return
            if resp.status_code == 200:
                all_transactions.extend(resp.json().get("transactions", []))
        except Exception as e:
            _log.debug(f"Whale Alert main fetch failed: {e}")

        await asyncio.sleep(1)

        # Second poll at lower threshold to catch more coins
        # Instead of querying each coin individually (hits rate limit),
        # do one bulk query at $50K minimum — catches all coins
        try:
            resp = await client.get(WHALE_ALERT_URL, params={
                "api_key": WHALE_ALERT_KEY,
                "min_value": LOW_THRESHOLD_MIN,
                "start": start,
                "limit": 100,
            }, timeout=15)
            if resp.status_code == 200:
                existing_hashes = {t.get("hash") for t in all_transactions}
                bulk_txns = resp.json().get("transactions", [])
                for t in bulk_txns:
                    if t.get("hash") not in existing_hashes:
                        all_transactions.append(t)
                        existing_hashes.add(t.get("hash"))
                if bulk_txns:
                    _log.info(f"Bulk $50K+ query returned {len(bulk_txns)} transactions")
            elif resp.status_code == 429:
                _log.warning("Whale Alert rate limited on bulk query")
        except Exception as e:
            _log.debug(f"Bulk whale query failed: {e}")

    transactions = all_transactions
    if not transactions:
        return

    async with async_session() as db:
        existing = set((await db.execute(
            select(WhaleTransaction.tx_hash)
        )).scalars().all())

        new_count = 0
        for t in transactions:
            tx_hash = t.get("hash", "")
            if not tx_hash or tx_hash in existing:
                continue

            symbol = (t.get("symbol") or "").lower()
            if symbol not in TRACKED_SYMBOLS:
                continue

            from_info = t.get("from", {})
            to_info = t.get("to", {})
            from_owner = from_info.get("owner", "") or ""
            to_owner = to_info.get("owner", "") or ""
            tx_type = _classify_tx(from_owner, to_owner)
            trade_time = datetime.utcfromtimestamp(t.get("timestamp", 0))

            whale = WhaleTransaction(
                tx_hash=tx_hash,
                blockchain=t.get("blockchain", ""),
                symbol=symbol.upper(),
                amount=Decimal(str(t.get("amount", 0))),
                amount_usd=Decimal(str(t.get("amount_usd", 0))),
                from_owner=from_owner or "unknown",
                from_address=from_info.get("address", "")[:128],
                to_owner=to_owner or "unknown",
                to_address=to_info.get("address", "")[:128],
                tx_type=tx_type,
                trade_time=trade_time,
            )
            db.add(whale)
            existing.add(tx_hash)
            new_count += 1

        if new_count:
            await db.commit()
            _log.info(f"Stored {new_count} new whale transactions")

            # Check for significant exchange deposits (sell pressure signal)
            for t in transactions:
                to_owner = (t.get("to", {}).get("owner") or "").lower()
                usd = t.get("amount_usd", 0)
                sym = (t.get("symbol") or "").upper()
                if to_owner in CEX_NAMES and usd >= 5_000_000:
                    _log.warning(f"WHALE ALERT: ${usd:,.0f} {sym} deposited to {to_owner} — potential sell pressure")
                    from ..models.platform import Notification
                    notif = Notification(
                        agent_id="0xb18a31796ea51c52c203c96aab0b1bc551c4e051",
                        type="whale_alert",
                        title=f"Whale: ${usd/1e6:.1f}M {sym} -> {to_owner.title()}",
                        body=f"${usd:,.0f} {sym} deposited to {to_owner.title()}. Potential sell pressure.",
                        link="/trading.html",
                    )
                    async with async_session() as db2:
                        db2.add(notif)
                        await db2.commit()


class CryptoSignal(Base):
    __tablename__ = "crypto_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    strength: Mapped[str] = mapped_column(String(20), default="MEDIUM")
    tx_count: Mapped[int] = mapped_column(Integer, default=0)
    total_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    exchanges_involved: Mapped[str | None] = mapped_column(Text, nullable=True)
    transactions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_crypto_signal_time", "detected_at"),
        Index("idx_crypto_signal_symbol", "symbol"),
    )


def _calc_crypto_signal_strength(tx_count: int, total_usd: float, exchange_count: int) -> str:
    if total_usd >= 50_000_000 and tx_count >= 3:
        return "VERY_STRONG"
    if total_usd >= 20_000_000 or (tx_count >= 3 and total_usd >= 5_000_000):
        return "STRONG"
    if total_usd >= 5_000_000 or tx_count >= 2:
        return "MEDIUM"
    return "WEAK"


async def _detect_crypto_signals():
    """Detect buy/sell pressure signals from whale exchange flows."""
    import json as _json
    from collections import defaultdict
    from datetime import timedelta

    async with async_session() as db:
        cutoff = datetime.utcnow() - timedelta(hours=6)

        # Get recent exchange deposits and withdrawals (exclude stablecoins from signals)
        stables_upper = {s.upper() for s in STABLECOINS}
        recent = (await db.execute(
            select(WhaleTransaction)
            .where(WhaleTransaction.trade_time >= cutoff,
                   WhaleTransaction.tx_type.in_(["exchange_deposit", "exchange_withdrawal"]),
                   ~WhaleTransaction.symbol.in_(stables_upper))
            .order_by(WhaleTransaction.trade_time.desc())
        )).scalars().all()

        if not recent:
            return

        # Group by symbol — net deposits vs withdrawals
        symbol_data = defaultdict(lambda: {"deposits": [], "withdrawals": [], "exchanges": set()})
        for t in recent:
            sd = symbol_data[t.symbol]
            if t.tx_type == "exchange_deposit":
                sd["deposits"].append(t)
                if t.to_owner and t.to_owner != "unknown":
                    sd["exchanges"].add(t.to_owner)
            else:
                sd["withdrawals"].append(t)
                if t.from_owner and t.from_owner != "unknown":
                    sd["exchanges"].add(t.from_owner)

        new_signals = 0
        for symbol, data in symbol_data.items():
            deposit_usd = sum(float(t.amount_usd) for t in data["deposits"])
            withdrawal_usd = sum(float(t.amount_usd) for t in data["withdrawals"])
            total_txns = len(data["deposits"]) + len(data["withdrawals"])

            if total_txns < 2:
                continue

            # Net direction — only create ONE signal per coin
            net = deposit_usd - withdrawal_usd
            if net > 0:
                direction = "SELL"
                total_usd = net
                signal_type = "sell_pressure"
            else:
                direction = "BUY"
                total_usd = abs(net)
                signal_type = "accumulation"

            # Skip if net flow is insignificant
            if total_usd < 1_000_000:
                continue

            # Check for existing signal for this coin in last 6 hours
            existing = (await db.execute(
                select(CryptoSignal)
                .where(CryptoSignal.symbol == symbol,
                       CryptoSignal.detected_at >= cutoff)
            )).scalar_one_or_none()
            if existing:
                continue

            exchanges = data["exchanges"]
            strength = _calc_crypto_signal_strength(total_txns, total_usd, len(exchanges))
            if strength == "WEAK":
                continue

            if direction == "SELL":
                desc = f"${symbol}: Net ${total_usd:,.0f} flowing INTO exchanges ({len(data['deposits'])} deposits vs {len(data['withdrawals'])} withdrawals). Exchanges: {', '.join(e.title() for e in exchanges)}."
            else:
                desc = f"${symbol}: Net ${total_usd:,.0f} flowing OUT of exchanges ({len(data['withdrawals'])} withdrawals vs {len(data['deposits'])} deposits). Exchanges: {', '.join(e.title() for e in exchanges)}. Whales accumulating."

            all_txns = data["deposits"] + data["withdrawals"]
            tx_info = [{"hash": t.tx_hash[:16]+"...", "amount_usd": float(t.amount_usd),
                        "from": t.from_owner, "to": t.to_owner,
                        "time": t.trade_time.isoformat()} for t in all_txns[:10]]

            signal = CryptoSignal(
                symbol=symbol,
                signal_type=signal_type,
                direction=direction,
                strength=strength,
                tx_count=total_txns,
                total_usd=Decimal(str(total_usd)),
                exchanges_involved=",".join(exchanges),
                transactions_json=_json.dumps(tx_info),
                description=desc,
            )
            db.add(signal)
            new_signals += 1

            _log.warning(f"CRYPTO SIGNAL [{strength}] {direction}: ${symbol} — {desc[:100]}")

            from ..models.platform import Notification
            notif = Notification(
                agent_id="0xb18a31796ea51c52c203c96aab0b1bc551c4e051",
                type="crypto_signal",
                title=f"Crypto {direction} Signal [{strength}]: ${symbol}",
                body=desc[:200],
                link="/trading.html",
            )
            db.add(notif)

        if new_signals:
            await db.commit()
            _log.info(f"Detected {new_signals} new crypto signals")


async def run():
    _log.info("Whale tracker starting — monitoring large crypto transfers")
    await asyncio.sleep(15)
    while True:
        try:
            await _poll_whale_alert()
            await _detect_crypto_signals()
        except Exception as e:
            _log.error(f"Whale tracker error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

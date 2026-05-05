# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Momentum & Volume Scanner — detects coins with unusual volume spikes and rapid price moves.
Catches breakouts that other data sources miss (e.g., ZEC, privacy coins, mid-caps).
Uses CoinGecko free API — no auth needed.
"""
import asyncio
import logging
import os
import json as _json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("momentum-scanner")

POLL_INTERVAL = 300  # 5 minutes — CoinGecko rate limit friendly
STABLECOINS = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "FDUSD", "USDD", "PYUSD", "GUSD", "FRAX"}
WRAPPED = {"WBTC", "WETH", "STETH", "WSTETH", "CBETH", "RETH"}


# === DB MODELS ===

class MomentumSignal(Base):
    __tablename__ = "momentum_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    coingecko_id: Mapped[str] = mapped_column(String(100), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    strength: Mapped[str] = mapped_column(String(20), default="MEDIUM")
    price: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    market_cap: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    volume_24h: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    volume_ratio: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    pct_change_1h: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    pct_change_24h: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    pct_change_7d: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_momentum_time", "detected_at"),
        Index("idx_momentum_symbol", "symbol"),
        Index("idx_momentum_type", "signal_type"),
    )


class VolumeBaseline(Base):
    __tablename__ = "volume_baselines"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    coingecko_id: Mapped[str] = mapped_column(String(100), nullable=False)
    avg_volume_7d: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    avg_mc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# === CONFIG ===

DEFAULT_CONFIG = {
    "volume_spike_threshold": 2.0,
    "strong_volume_threshold": 3.0,
    "very_strong_volume_threshold": 5.0,
    "min_price_change_1h": 3.0,
    "min_price_change_24h": 8.0,
    "strong_price_change_24h": 15.0,
    "min_market_cap": 10_000_000,
    "max_market_cap": 50_000_000_000,
    "min_volume_24h": 5_000_000,
    "cooldown_hours": 6,
    "top_n_coins": 250,
}


async def get_config() -> dict:
    try:
        from ..core.redis import redis_client
        stored = await redis_client.get("momentum_scanner_config")
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
        await redis_client.set("momentum_scanner_config", _json.dumps(current))
    except:
        pass


# === DATA FETCHING ===

async def _fetch_market_data(top_n: int = 250) -> list:
    coins = []
    per_page = min(top_n, 250)
    pages = (top_n + per_page - 1) // per_page

    async with httpx.AsyncClient() as client:
        for page in range(1, pages + 1):
            try:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={
                        "vs_currency": "usd",
                        "order": "market_cap_desc",
                        "per_page": per_page,
                        "page": page,
                        "sparkline": "false",
                        "price_change_percentage": "1h,24h,7d",
                    },
                    timeout=15,
                )
                if resp.status_code == 200:
                    coins.extend(resp.json())
                elif resp.status_code == 429:
                    _log.warning("CoinGecko rate limited, using partial data")
                    break
                await asyncio.sleep(2)
            except Exception as e:
                _log.debug(f"CoinGecko fetch error page {page}: {e}")
                break

    return coins


# === BASELINE MANAGEMENT ===

async def _update_baselines(coins: list):
    async with async_session() as db:
        for coin in coins:
            symbol = (coin.get("symbol") or "").upper()
            cg_id = coin.get("id", "")
            volume = float(coin.get("total_volume") or 0)
            mc = float(coin.get("market_cap") or 0)

            if not symbol or volume <= 0:
                continue

            existing = (await db.execute(
                select(VolumeBaseline).where(VolumeBaseline.symbol == symbol)
            )).scalar_one_or_none()

            if existing:
                # Exponential moving average — weight recent data more
                count = min(existing.sample_count + 1, 288)  # ~24h at 5min intervals
                alpha = 2 / (count + 1)
                new_avg = alpha * volume + (1 - alpha) * float(existing.avg_volume_7d)
                existing.avg_volume_7d = Decimal(str(round(new_avg, 2)))
                existing.avg_mc = Decimal(str(round(mc, 2)))
                existing.sample_count = count
                existing.updated_at = datetime.utcnow()
            else:
                baseline = VolumeBaseline(
                    symbol=symbol,
                    coingecko_id=cg_id,
                    avg_volume_7d=Decimal(str(round(volume, 2))),
                    avg_mc=Decimal(str(round(mc, 2))),
                    sample_count=1,
                )
                db.add(baseline)

        await db.commit()


# === SIGNAL DETECTION ===

async def _scan_for_signals(coins: list):
    config = await get_config()

    async with async_session() as db:
        for coin in coins:
            symbol = (coin.get("symbol") or "").upper()
            cg_id = coin.get("id", "")
            price = float(coin.get("current_price") or 0)
            mc = float(coin.get("market_cap") or 0)
            volume = float(coin.get("total_volume") or 0)
            pct_1h = float(coin.get("price_change_percentage_1h_in_currency") or 0)
            pct_24h = float(coin.get("price_change_percentage_24h_in_currency") or 0)
            pct_7d = float(coin.get("price_change_percentage_7d_in_currency") or 0)

            if symbol in STABLECOINS or symbol in WRAPPED:
                continue
            if mc < config["min_market_cap"] or mc > config["max_market_cap"]:
                continue
            if volume < config["min_volume_24h"]:
                continue
            if price <= 0:
                continue

            # Get baseline volume
            baseline = (await db.execute(
                select(VolumeBaseline).where(VolumeBaseline.symbol == symbol)
            )).scalar_one_or_none()

            if not baseline or baseline.sample_count < 3:
                continue

            avg_vol = float(baseline.avg_volume_7d)
            if avg_vol <= 0:
                continue

            volume_ratio = volume / avg_vol

            # Check cooldown — don't signal same coin within N hours
            cutoff = datetime.utcnow() - timedelta(hours=config["cooldown_hours"])
            recent = (await db.execute(
                select(func.count()).select_from(MomentumSignal)
                .where(MomentumSignal.symbol == symbol,
                       MomentumSignal.detected_at >= cutoff)
            )).scalar() or 0
            if recent > 0:
                continue

            signals_to_create = []

            # VOLUME SPIKE detection
            if volume_ratio >= config["volume_spike_threshold"]:
                if volume_ratio >= config["very_strong_volume_threshold"]:
                    strength = "VERY_STRONG"
                elif volume_ratio >= config["strong_volume_threshold"]:
                    strength = "STRONG"
                else:
                    strength = "MEDIUM"

                direction = "BULLISH" if pct_24h > 0 else "BEARISH"
                signals_to_create.append({
                    "signal_type": "volume_spike",
                    "strength": strength,
                    "description": (
                        f"${symbol} volume {volume_ratio:.1f}x above average "
                        f"(${volume:,.0f} vs avg ${avg_vol:,.0f}). "
                        f"Price {'+' if pct_24h > 0 else ''}{pct_24h:.1f}% 24h. {direction}."
                    ),
                })

            # PRICE MOMENTUM detection
            if abs(pct_1h) >= config["min_price_change_1h"]:
                direction = "up" if pct_1h > 0 else "down"
                strength = "STRONG" if abs(pct_1h) >= 5 else "MEDIUM"
                if abs(pct_1h) >= 8:
                    strength = "VERY_STRONG"

                signals_to_create.append({
                    "signal_type": f"momentum_{direction}",
                    "strength": strength,
                    "description": (
                        f"${symbol} moving {direction} {abs(pct_1h):.1f}% in 1h. "
                        f"24h: {'+' if pct_24h > 0 else ''}{pct_24h:.1f}%, "
                        f"7d: {'+' if pct_7d > 0 else ''}{pct_7d:.1f}%. "
                        f"Vol ratio: {volume_ratio:.1f}x."
                    ),
                })

            # BREAKOUT detection — strong 24h move + volume confirmation
            if pct_24h >= config["strong_price_change_24h"] and volume_ratio >= 1.5:
                strength = "VERY_STRONG" if pct_24h >= 25 and volume_ratio >= 3 else "STRONG"
                signals_to_create.append({
                    "signal_type": "breakout",
                    "strength": strength,
                    "description": (
                        f"${symbol} BREAKOUT: +{pct_24h:.1f}% 24h with "
                        f"{volume_ratio:.1f}x volume. MC: ${mc:,.0f}."
                    ),
                })

            # DUMP detection — sharp drop with volume
            if pct_24h <= -config["strong_price_change_24h"] and volume_ratio >= 1.5:
                strength = "VERY_STRONG" if pct_24h <= -25 and volume_ratio >= 3 else "STRONG"
                signals_to_create.append({
                    "signal_type": "dump",
                    "strength": strength,
                    "description": (
                        f"${symbol} DUMP: {pct_24h:.1f}% 24h with "
                        f"{volume_ratio:.1f}x volume. MC: ${mc:,.0f}."
                    ),
                })

            for sig_data in signals_to_create:
                signal = MomentumSignal(
                    symbol=symbol,
                    coingecko_id=cg_id,
                    signal_type=sig_data["signal_type"],
                    strength=sig_data["strength"],
                    price=Decimal(str(price)),
                    market_cap=Decimal(str(round(mc, 2))),
                    volume_24h=Decimal(str(round(volume, 2))),
                    volume_ratio=Decimal(str(round(volume_ratio, 2))),
                    pct_change_1h=Decimal(str(round(pct_1h, 4))),
                    pct_change_24h=Decimal(str(round(pct_24h, 4))),
                    pct_change_7d=Decimal(str(round(pct_7d, 4))),
                    description=sig_data["description"],
                )
                db.add(signal)

                _log.info(f"MOMENTUM: {sig_data['signal_type']} {sig_data['strength']} ${symbol} — {sig_data['description'][:80]}")

                # Telegram alert for STRONG+ signals
                try:
                    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                    if bot_token and chat_id and sig_data["strength"] in ("STRONG", "VERY_STRONG"):
                        emoji = {
                            "volume_spike": "🔊",
                            "momentum_up": "🚀",
                            "momentum_down": "📉",
                            "breakout": "💥",
                            "dump": "🔻",
                        }.get(sig_data["signal_type"], "⚡")

                        msg = (
                            f"{emoji} *{sig_data['signal_type'].upper().replace('_', ' ')}: ${symbol}*\n\n"
                            f"Price: ${price:,.4f}\n"
                            f"1h: {'+' if pct_1h > 0 else ''}{pct_1h:.1f}%\n"
                            f"24h: {'+' if pct_24h > 0 else ''}{pct_24h:.1f}%\n"
                            f"Volume: ${volume:,.0f} ({volume_ratio:.1f}x avg)\n"
                            f"MC: ${mc:,.0f}\n\n"
                            f"[Agiotage](https://agiotage.finance/trading.html)"
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

        await db.commit()


# === MAIN LOOP ===

async def run():
    _log.info("Momentum & Volume Scanner starting")
    await asyncio.sleep(75)

    config = await get_config()
    _log.info(f"Config: vol_spike={config['volume_spike_threshold']}x, "
              f"min_1h={config['min_price_change_1h']}%, "
              f"min_24h={config['min_price_change_24h']}%, "
              f"MC={config['min_market_cap']:,.0f}-{config['max_market_cap']:,.0f}")

    while True:
        try:
            coins = await _fetch_market_data(config.get("top_n_coins", 250))
            if coins:
                await _update_baselines(coins)
                await _scan_for_signals(coins)
                _log.debug(f"Scanned {len(coins)} coins")
        except Exception as e:
            _log.error(f"Momentum scanner error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

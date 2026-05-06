# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Agiotage Crypto Trading Bot v2 — tier-based position sizing, market regime detection,
funding rate checks, Fear & Greed contrarian signals, time-decayed entry scoring,
tier-specific TP/SL, trailing stops, breakeven stops, daily loss limits, Kraken live trading.
All parameters adjustable via API without redeploying.
"""
import asyncio
import logging
import os
import json as _json
import math
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
REGIME_UPDATE_INTERVAL = 300  # 5 minutes


# === DEFAULT CONFIG (adjustable via Redis) ===
DEFAULT_CONFIG = {
    # Entry criteria
    "min_confidence": 50,
    "min_sources": 2,
    "signal_lookback_minutes": 60,

    # Position sizing (tier-based)
    "position_usd_tier2": 20,      # SOL, large alts (live)
    "position_usd_tier3": 15,      # Mid-cap alts (live)
    "position_usd_paper": 500,     # Paper-only coins
    "max_open_positions": 5,
    "daily_loss_limit_usd": 50,

    # Market regime
    "require_bull_regime_for_alts": True,
    "bear_position_multiplier": 0.5,

    # Take profit -- Large alts (SOL, SUI, APT)
    "tp_levels_large": [
        {"sell_pct": 20, "at_profit_pct": 4},
        {"sell_pct": 20, "at_profit_pct": 10},
        {"sell_pct": 30, "at_profit_pct": 18},
        {"sell_pct": 30, "at_profit_pct": 35},
    ],
    # Take profit -- Mid-cap (WIF, JUP, BONK)
    "tp_levels_midcap": [
        {"sell_pct": 15, "at_profit_pct": 5},
        {"sell_pct": 15, "at_profit_pct": 12},
        {"sell_pct": 35, "at_profit_pct": 25},
        {"sell_pct": 35, "at_profit_pct": 50},
    ],

    # Stop loss -- by tier
    "stop_loss_large": 7,
    "stop_loss_midcap": 10,
    "trailing_stop_enabled": True,
    "trailing_activation_large": 6,
    "trailing_trail_large": 7,
    "trailing_activation_midcap": 4,
    "trailing_trail_midcap": 9,
    "breakeven_stop_after_first_tp": True,
    "max_holding_hours_large": 48,
    "max_holding_hours_midcap": 36,

    # Time-based stop tightening
    "tighten_sl_after_hours_large": 18,
    "tighten_sl_to_large": 4,
    "tighten_sl_after_hours_midcap": 12,
    "tighten_sl_to_midcap": 6,

    # Coin tiers
    "tier1_coins": ["BTC", "ETH"],
    "tier2_coins": ["SOL", "AVAX", "LINK", "SUI", "APT", "NEAR", "ARB", "OP", "INJ", "TIA", "SEI", "AAVE", "RENDER"],
    "tier3_coins": ["WIF", "JUP", "BONK", "PEPE", "DOGE", "FET", "TAO", "ONDO", "ADA", "DOT", "MATIC"],
    "skip_symbols": ["USDC", "USDT", "DAI", "BUSD"],

    # Execution
    "allowed_symbols": [
        "BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "ADA", "DOT", "MATIC",
        "NEAR", "ARB", "OP", "SUI", "APT", "INJ", "TIA", "SEI", "JUP",
        "WIF", "PEPE", "RENDER", "FET", "TAO", "ONDO", "AAVE", "BONK",
    ],
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


# === DAILY LOSS TRACKING ===

async def _track_daily_loss(loss_usd: float):
    try:
        from ..core.redis import redis_client
        key = f"crypto_trader:daily_loss:{datetime.utcnow().strftime('%Y-%m-%d')}"
        current = float(await redis_client.get(key) or 0)
        await redis_client.set(key, str(current + loss_usd), ex=86400)
    except:
        pass


async def _get_daily_loss() -> float:
    try:
        from ..core.redis import redis_client
        key = f"crypto_trader:daily_loss:{datetime.utcnow().strftime('%Y-%m-%d')}"
        return float(await redis_client.get(key) or 0)
    except:
        return 9999  # Redis down = assume limit hit, block new entries


# === PRICE FETCHING ===

CG_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "AVAX": "avalanche-2",
    "LINK": "chainlink", "DOGE": "dogecoin", "ADA": "cardano", "DOT": "polkadot",
    "MATIC": "matic-network", "NEAR": "near", "ARB": "arbitrum", "OP": "optimism",
    "SUI": "sui", "APT": "aptos", "INJ": "injective-protocol", "TIA": "celestia",
    "SEI": "sei-network", "JUP": "jupiter-exchange-solana", "WIF": "dogwifcoin",
    "PEPE": "pepe", "RENDER": "render-token", "FET": "fetch-ai", "TAO": "bittensor",
    "ONDO": "ondo-finance", "AAVE": "aave", "BONK": "bonk",
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


# === TELEGRAM ALERT ===

async def _send_telegram(msg: str):
    """Send a Telegram message. Silently fails if not configured."""
    try:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if bot_token and chat_id:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown",
                          "disable_web_page_preview": True}, timeout=5)
    except:
        pass


# === COIN TIER DETECTION ===

def _get_coin_tier(symbol: str, config: dict) -> int:
    """Return coin tier: 1 = BTC/ETH (no trade), 2 = large alts, 3 = mid-cap."""
    s = symbol.upper()
    if s in config.get("tier1_coins", []):
        return 1
    if s in config.get("tier2_coins", []):
        return 2
    if s in config.get("tier3_coins", []):
        return 3
    return 3  # default to most conservative


# === MARKET REGIME DETECTION ===

async def _get_market_regime() -> str:
    """Detect bull/bear/range using BTC price vs 20-EMA + whale flow."""
    try:
        from ..core.redis import redis_client
        regime = await redis_client.get("crypto:market_regime")
        if regime:
            return regime
    except:
        pass
    return "unknown"


async def _update_market_regime() -> str:
    """Update market regime. Call every cycle (cached 5 min in Redis)."""
    try:
        async with httpx.AsyncClient() as client:
            # BTC hourly prices for EMA
            resp = await client.get(
                "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
                params={"vs_currency": "usd", "days": "2"}, timeout=10)
            if resp.status_code == 200:
                prices = resp.json().get("prices", [])
                if len(prices) >= 20:
                    # Calculate 20-period EMA from hourly data
                    hourly = [p[1] for p in prices[-48:]]  # last 48 hours
                    ema = hourly[0]
                    k = 2 / (20 + 1)
                    for p in hourly[1:]:
                        ema = p * k + ema * (1 - k)

                    current = hourly[-1]
                    pct_from_ema = ((current - ema) / ema) * 100

                    if pct_from_ema > 3:
                        regime = "bull"
                    elif pct_from_ema < -3:
                        regime = "bear"
                    else:
                        regime = "range"

                    from ..core.redis import redis_client
                    await redis_client.set("crypto:market_regime", regime, ex=600)
                    _log.info(f"Market regime: {regime} (BTC {pct_from_ema:+.2f}% from 20-EMA)")
                    return regime
    except:
        pass
    return "unknown"


# === FUNDING RATE CHECK (Binance, FREE) ===

async def _get_funding_rate(symbol: str) -> float | None:
    """Get Binance perpetual funding rate. High positive = overleveraged longs."""
    try:
        pair = f"{symbol.upper()}USDT"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": pair, "limit": 1}, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    return float(data[0].get("fundingRate", 0))
    except:
        pass
    return None


# === FEAR & GREED INDEX (FREE) ===

async def _get_fear_greed() -> int | None:
    """Get Crypto Fear & Greed Index (0-100). <25 = extreme fear, >75 = extreme greed."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.alternative.me/fng/?limit=1", timeout=8)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    return int(data[0].get("value", 50))
    except:
        pass
    return None


# === LIVE TRADING HELPERS (Kraken) ===

async def _is_live_mode() -> bool:
    try:
        from ..core.redis import redis_client
        paused = await redis_client.get("trading:paused")
        if paused == "1":
            return False
        tc = await redis_client.get("trading:config")
        if tc:
            tc_data = _json.loads(tc)
            return tc_data.get("live_mode", False) and "crypto_trader" in tc_data.get("allowed_traders", [])
    except:
        pass
    return False


async def _live_buy(symbol: str, amount_usd: float) -> dict | None:
    """Execute a live buy via Kraken if live mode is on. Returns tx result or None."""
    if not await _is_live_mode():
        return None
    try:
        from ..services.kraken_exchange import buy
        result = await buy(symbol, amount_usd)
        if result.get("success"):
            _log.info(f"LIVE BUY: ${symbol} ${amount_usd:.2f} order={result.get('order_id')}")
        else:
            _log.error(f"LIVE BUY FAILED: ${symbol} -- {result.get('error')}")
        return result
    except Exception as e:
        _log.error(f"LIVE BUY ERROR: ${symbol} -- {e}")
        return None


async def _live_sell(symbol: str, amount_usd: float) -> dict | None:
    """Execute a live sell via Kraken if live mode is on. Returns tx result or None."""
    if not await _is_live_mode():
        return None
    try:
        from ..services.kraken_exchange import sell
        result = await sell(symbol, amount_usd)
        if result.get("success"):
            _log.info(f"LIVE SELL: ${symbol} ${amount_usd:.2f} order={result.get('order_id')}")
        else:
            _log.error(f"LIVE SELL FAILED: ${symbol} -- {result.get('error')}")
        return result
    except Exception as e:
        _log.error(f"LIVE SELL ERROR: ${symbol} -- {e}")
        return None


# === POSITION SIZE BY TIER ===

def _position_usd_for_tier(tier: int, config: dict, live_mode: bool, regime: str) -> float:
    """Return position size in USD based on coin tier, mode, and regime."""
    if not live_mode:
        base = config["position_usd_paper"]
    elif tier == 2:
        base = config["position_usd_tier2"]
    else:
        base = config["position_usd_tier3"]

    # Reduce size in bear regime
    if regime == "bear":
        base *= config.get("bear_position_multiplier", 0.5)

    return base


# === TIER-SPECIFIC CONFIG HELPERS ===

def _get_tp_levels(tier: int, config: dict) -> list:
    """Return take-profit levels for the given tier."""
    if tier == 2:
        return config.get("tp_levels_large", [])
    return config.get("tp_levels_midcap", [])


def _get_stop_loss_pct(tier: int, config: dict) -> float:
    """Return stop loss percentage for the given tier."""
    if tier == 2:
        return config.get("stop_loss_large", 7)
    return config.get("stop_loss_midcap", 10)


def _get_trailing_config(tier: int, config: dict) -> tuple:
    """Return (activation_pct, trail_pct) for the given tier."""
    if tier == 2:
        return config.get("trailing_activation_large", 6), config.get("trailing_trail_large", 7)
    return config.get("trailing_activation_midcap", 4), config.get("trailing_trail_midcap", 9)


def _get_max_holding_hours(tier: int, config: dict) -> float:
    """Return max holding hours for the given tier."""
    if tier == 2:
        return config.get("max_holding_hours_large", 48)
    return config.get("max_holding_hours_midcap", 36)


def _get_tighten_sl_config(tier: int, config: dict) -> tuple:
    """Return (tighten_after_hours, tighten_to_pct) for the given tier."""
    if tier == 2:
        return config.get("tighten_sl_after_hours_large", 18), config.get("tighten_sl_to_large", 4)
    return config.get("tighten_sl_after_hours_midcap", 12), config.get("tighten_sl_to_midcap", 6)


# === ENTRY SCORING ===

def _time_decay(value: float, age_minutes: float, decay_pct_per_hour: float) -> float:
    """Apply time-based decay to a score value."""
    if age_minutes <= 0:
        return value
    decay_per_min = decay_pct_per_hour / 60.0
    factor = max(0, 1.0 - (decay_per_min * age_minutes / 100.0))
    return value * factor


def _time_decay_pts(value: float, age_minutes: float, pts_per_15min: float) -> float:
    """Apply time-based decay in points per 15 minutes."""
    if age_minutes <= 0:
        return value
    lost = pts_per_15min * (age_minutes / 15.0)
    return max(0, value - lost)


# === ENTRY LOGIC ===

async def _check_for_entries():
    config = await get_config()
    regime = await _get_market_regime()

    async with async_session() as db:
        # 1. Count open positions
        open_count = (await db.execute(
            select(func.count()).select_from(CryptoPaperPosition)
            .where(CryptoPaperPosition.status == "OPEN")
        )).scalar() or 0

        if open_count >= config["max_open_positions"]:
            return

        # 2. Check daily loss limit
        daily_loss = await _get_daily_loss()
        if daily_loss >= config["daily_loss_limit_usd"]:
            _log.warning(f"Daily loss limit reached: ${daily_loss:.2f} >= ${config['daily_loss_limit_usd']}")
            return

        # 3. Get recent signals
        from .whale_tracker import CryptoSignal
        lookback = config.get("signal_lookback_minutes", 60)
        cutoff = datetime.utcnow() - timedelta(minutes=lookback)

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

        # Fetch fear & greed once per cycle
        fear_greed = await _get_fear_greed()

        for symbol, data in symbol_data.items():
            tier = _get_coin_tier(symbol, config)

            # Skip tier 1 (BTC/ETH) -- observation only
            if tier == 1:
                continue

            # Regime gate: skip alts in bear regime if configured
            if config.get("require_bull_regime_for_alts", True) and tier == 3 and regime == "bear":
                _log.info(f"SKIP ${symbol}: bear regime, no alt entries")
                continue

            # Check if we already have an open position in this symbol
            existing = (await db.execute(
                select(CryptoPaperPosition)
                .where(CryptoPaperPosition.ticker == symbol,
                       CryptoPaperPosition.status == "OPEN")
            )).scalar_one_or_none()
            if existing:
                continue

            # Signal dedup: check if we opened a position for this symbol in the last 5 minutes
            recent_entry = (await db.execute(
                select(CryptoPaperPosition)
                .where(CryptoPaperPosition.ticker == symbol,
                       CryptoPaperPosition.opened_at >= datetime.utcnow() - timedelta(minutes=5))
            )).scalar_one_or_none()
            if recent_entry:
                continue

            # === ENTRY SCORING (time-decayed, multi-source) ===
            score = 0
            sources = []
            now = datetime.utcnow()

            # Track signal timestamps for echo chamber detection
            signal_timestamps = []

            # -- Whale flow scoring --
            if data["whale_signals"]:
                sources.append("whale_flow")
                best_whale = data["whale_signals"][0]
                age_min = (now - best_whale.detected_at).total_seconds() / 60.0
                signal_timestamps.append(best_whale.detected_at)

                whale_usd = float(best_whale.total_usd) if best_whale.total_usd else 0
                if whale_usd > 10_000_000:
                    score += _time_decay(35, age_min, 30)  # +35, decay -30%/hour
                elif whale_usd > 5_000_000:
                    score += _time_decay(20, age_min, 30)
                elif best_whale.strength == "VERY_STRONG":
                    score += _time_decay(20, age_min, 30)
                elif best_whale.strength == "STRONG":
                    score += _time_decay(15, age_min, 30)

                # Fear & Greed contrarian bonus: extreme fear + whale accumulation
                if fear_greed is not None and fear_greed < 25 and whale_usd > 5_000_000:
                    score += 15
                    sources.append("contrarian")

            # -- Momentum signal scoring --
            if data["momentum"]:
                sources.append("momentum")
                best_mom = data["momentum"][0]
                age_min = (now - best_mom.detected_at).total_seconds() / 60.0
                signal_timestamps.append(best_mom.detected_at)

                if best_mom.strength == "VERY_STRONG":
                    score += _time_decay_pts(20, age_min, 5)  # +20, decay -5pts/15min
                elif best_mom.strength == "STRONG":
                    score += _time_decay_pts(15, age_min, 5)

                # Volume accumulation bonus: 3x volume with <2% price move
                if best_mom.signal_type == "volume_spike":
                    vol_ratio = float(best_mom.volume_ratio) if best_mom.volume_ratio else 0
                    if vol_ratio >= 3:
                        # Check if price move is small (accumulation pattern)
                        try:
                            price_change = float(best_mom.price_change_pct) if hasattr(best_mom, 'price_change_pct') and best_mom.price_change_pct else 999
                        except:
                            price_change = 999
                        if abs(price_change) < 2:
                            score += 15
                            if "vol_accum" not in sources:
                                sources.append("vol_accum")

                if best_mom.signal_type == "breakout":
                    score += 5

            # -- Correlated signal scoring --
            if data["correlated"]:
                sources.append("correlated")
                best_corr = max(data["correlated"], key=lambda s: s.confidence)
                age_min = (now - best_corr.detected_at).total_seconds() / 60.0
                signal_timestamps.append(best_corr.detected_at)

                conf = best_corr.confidence
                if conf >= 70:
                    score += _time_decay(conf, age_min, 25)  # full confidence, decay -25%/hr
                elif conf >= 50:
                    score += _time_decay(conf / 2, age_min, 25)  # half confidence

            # -- LunarCrush Galaxy Score --
            try:
                lc_key = os.getenv("LUNARCRUSH_API_KEY", "")
                if lc_key and symbol:
                    async with httpx.AsyncClient() as client:
                        lc_resp = await client.get(
                            f"https://lunarcrush.com/api4/public/coins/{symbol}/v1",
                            headers={"Authorization": f"Bearer {lc_key}"}, timeout=8)
                        if lc_resp.status_code == 200:
                            lc_data = lc_resp.json().get("data", {})
                            gs = lc_data.get("galaxy_score")
                            if gs and float(gs) >= 70:
                                score += 18
                                sources.append("lunarcrush")
            except:
                pass

            # -- Social sentiment (3+ platforms, bullish) --
            try:
                from .sentiment_tracker import SocialMention
                sent_count = (await db.execute(
                    select(func.count()).select_from(SocialMention)
                    .where(SocialMention.token_symbol == symbol,
                           SocialMention.detected_at >= datetime.utcnow() - timedelta(hours=6))
                )).scalar() or 0
                if sent_count >= 3:
                    score += 10
                    sources.append("sentiment")
            except:
                pass

            # -- Funding rate penalty --
            funding = await _get_funding_rate(symbol)
            if funding is not None and funding > 0.001:  # 0.1%
                score -= 10
                sources.append("funding_penalty")

            # -- Echo chamber detection: all sources within 5 minutes of each other --
            if len(signal_timestamps) >= 3:
                ts_sorted = sorted(signal_timestamps)
                span_minutes = (ts_sorted[-1] - ts_sorted[0]).total_seconds() / 60.0
                if span_minutes <= 5:
                    score -= 15
                    sources.append("echo_penalty")

            # Round final score
            score = int(round(score))

            # Check min requirements
            if score < config["min_confidence"]:
                continue
            if len([s for s in sources if not s.endswith("_penalty")]) < config["min_sources"]:
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

            # Determine position size
            live_mode = await _is_live_mode()
            position_usd = _position_usd_for_tier(tier, config, live_mode, regime)

            # === LIVE BUY (Kraken) ===
            live_result = None
            if live_mode:
                # Re-check kill switch right before execution
                try:
                    from ..core.redis import redis_client
                    if (await redis_client.get("trading:paused")) == "1":
                        live_mode = False
                except:
                    live_mode = False

            if live_mode:
                live_result = await _live_buy(symbol, position_usd)
                if live_result and not live_result.get("success"):
                    # Don't create position if live buy fails -- we don't own the tokens
                    _log.error(f"SKIP ${symbol}: live buy failed, not creating position")
                    continue
                elif not live_result:
                    # _live_buy returned None (live mode turned off mid-check)
                    live_mode = False

            # === RECORD POSITION AND TRADE ===
            signal_str = data["whale_signals"][0].strength if data["whale_signals"] else "CORRELATED"
            live_tag = ""
            if live_result and live_result.get("success"):
                order_id = live_result.get("order_id", "")
                live_tag = f" [LIVE:{order_id[:12]}]" if order_id else " [LIVE]"

            position = CryptoPaperPosition(
                ticker=symbol,
                token_symbol=symbol,
                entry_price=Decimal(str(price)),
                position_size_usd=Decimal(str(round(position_usd, 2))),
                current_price=Decimal(str(price)),
                highest_price=Decimal(str(price)),
                pnl_pct=Decimal("0"),
                pnl_usd=Decimal("0"),
                entry_signal=signal_str,
                entry_sources=",".join(sources),
                score=score,
            )
            db.add(position)

            reason = f"Signal: {signal_str}, Score: {score}, T{tier}{live_tag}"
            trade = CryptoPaperTrade(
                position_id=0,
                action="BUY",
                pct_of_position=100,
                price=Decimal(str(price)),
                usd_value=Decimal(str(round(position_usd, 2))),
                pnl_pct=Decimal("0"),
                reason=reason[:100],
            )

            await db.commit()
            await db.refresh(position)
            trade.position_id = position.id
            db.add(trade)
            await db.commit()

            mode_label = "LIVE" if live_result and live_result.get("success") else "PAPER"
            tier_label = "large" if tier == 2 else "midcap"
            _log.info(f"{mode_label} BUY: ${symbol} @ ${price:,.4f} Score={score} "
                       f"Size=${position_usd:.2f} Tier={tier_label} Regime={regime} [{','.join(sources)}]")

            # Telegram alert
            regime_emoji = {"bull": "B", "bear": "b", "range": "~"}.get(regime, "?")
            msg = (
                f"{'$' if mode_label == 'LIVE' else '#'} *{mode_label} BUY: ${symbol}*\n\n"
                f"Price: ${price:,.4f}\n"
                f"Size: ${position_usd:.2f}\n"
                f"Score: {score}\n"
                f"Tier: {tier_label} | Regime: {regime} [{regime_emoji}]\n"
                f"Sources: {', '.join(sources)}\n"
                f"F&G: {fear_greed or 'N/A'}"
            )
            await _send_telegram(msg)


# === POSITION MANAGEMENT ===

async def _manage_positions():
    config = await get_config()
    regime = await _get_market_regime()

    async with async_session() as db:
        positions = (await db.execute(
            select(CryptoPaperPosition).where(CryptoPaperPosition.status == "OPEN")
        )).scalars().all()

        for pos in positions:
            price = await _get_crypto_price(pos.ticker)
            if price <= 0:
                continue

            entry = float(pos.entry_price)
            if entry <= 0:
                continue

            tier = _get_coin_tier(pos.ticker, config)
            pnl_pct = ((price - entry) / entry) * 100
            pnl_usd = (pnl_pct / 100) * float(pos.position_size_usd) * (float(pos.remaining_pct) / 100)
            highest = max(float(pos.highest_price or price), price)

            pos.current_price = Decimal(str(price))
            pos.highest_price = Decimal(str(highest))
            pos.pnl_pct = Decimal(str(round(pnl_pct, 4)))
            pos.pnl_usd = Decimal(str(round(pnl_usd, 2)))
            pos.last_updated = datetime.utcnow()

            remaining = float(pos.remaining_pct)
            age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600

            # Get tier-specific configs
            tp_levels = _get_tp_levels(tier, config)
            base_sl_pct = _get_stop_loss_pct(tier, config)
            trail_activation, trail_pct = _get_trailing_config(tier, config)
            max_hold = _get_max_holding_hours(tier, config)
            tighten_after, tighten_to = _get_tighten_sl_config(tier, config)

            # Time-based stop tightening
            effective_sl = base_sl_pct
            if age_hours >= tighten_after:
                effective_sl = tighten_to

            # Regime-aware: if regime flipped to bear, tighten stops immediately
            if regime == "bear":
                effective_sl = min(effective_sl, tighten_to)

            # Check if first TP was already hit (for breakeven stop)
            first_tp_hit = False
            if config["breakeven_stop_after_first_tp"] and tp_levels:
                first_tp = tp_levels[0]
                existing_first_tp = (await db.execute(
                    select(CryptoPaperTrade)
                    .where(CryptoPaperTrade.position_id == pos.id,
                           CryptoPaperTrade.reason.contains(f"TP {first_tp['at_profit_pct']}%"))
                )).scalar_one_or_none()
                if existing_first_tp:
                    first_tp_hit = True

            # === CHECK TAKE PROFIT LEVELS ===
            for tp in tp_levels:
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

                    # Live sell if enabled
                    sell_usd = float(pos.position_size_usd) * (sell_pct / 100) * (1 + pnl_pct / 100)
                    live_tx = await _live_sell(pos.ticker, sell_usd)
                    tx_tag = f" [LIVE:{live_tx.get('order_id', '')[:12]}]" if live_tx and live_tx.get("success") else ""

                    trade = CryptoPaperTrade(
                        position_id=pos.id, action="SELL",
                        pct_of_position=sell_pct, price=Decimal(str(price)),
                        usd_value=Decimal(str(round(sell_usd, 2))),
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        reason=f"TP {tp['at_profit_pct']}% hit -- sold {sell_pct}%{tx_tag}"[:100],
                    )
                    db.add(trade)
                    pos.remaining_pct = Decimal(str(remaining - sell_pct))
                    remaining -= sell_pct

                    mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                    _log.info(f"{mode} SELL (TP): ${pos.ticker} {sell_pct}% @ +{pnl_pct:.1f}%")

                    await _send_telegram(
                        f"*{mode} SELL: ${pos.ticker}*\n"
                        f"TP {tp['at_profit_pct']}% hit\n"
                        f"Sold {sell_pct}% @ +{pnl_pct:.1f}%\n"
                        f"Value: ${sell_usd:.2f}"
                    )

                    # Mark first TP hit for breakeven stop
                    if not first_tp_hit:
                        first_tp_hit = True

            # === CHECK STOP LOSS (with breakeven stop adjustment) ===
            if first_tp_hit and config["breakeven_stop_after_first_tp"]:
                # After first TP hit, stop loss moves to entry + 2% (protect profits)
                sl_triggered = pnl_pct <= 2.0
                close_reason = f"Breakeven stop ({pnl_pct:.1f}%)"
            else:
                sl_triggered = pnl_pct <= -effective_sl
                close_reason = f"Stop loss ({pnl_pct:.1f}%, SL={effective_sl}%)"

            if sl_triggered and remaining > 0:
                sell_usd = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                live_tx = await _live_sell(pos.ticker, sell_usd)
                tx_tag = f" [LIVE:{live_tx.get('order_id', '')[:12]}]" if live_tx and live_tx.get("success") else ""

                trade = CryptoPaperTrade(
                    position_id=pos.id, action="SELL",
                    pct_of_position=remaining, price=Decimal(str(price)),
                    usd_value=Decimal(str(round(sell_usd, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=f"{close_reason}{tx_tag}"[:100],
                )
                db.add(trade)
                pos.remaining_pct = Decimal("0")
                pos.status = "CLOSED"
                pos.close_reason = close_reason[:50]
                pos.closed_at = datetime.utcnow()

                # Track daily loss
                if pnl_pct < 0:
                    loss_usd = abs(float(pos.position_size_usd) * (remaining / 100) * (pnl_pct / 100))
                    await _track_daily_loss(loss_usd)

                mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                _log.info(f"{mode} SELL (SL): ${pos.ticker} 100% @ {pnl_pct:.1f}%")
                await _send_telegram(
                    f"*{mode} STOP LOSS: ${pos.ticker}*\n"
                    f"{close_reason}\n"
                    f"Sold {remaining:.0f}% @ {pnl_pct:.1f}%\n"
                    f"Value: ${sell_usd:.2f}"
                )
                continue  # Position closed

            # === CHECK TRAILING STOP ===
            if config["trailing_stop_enabled"] and remaining > 0:
                highest_pnl = ((highest - entry) / entry) * 100
                if highest_pnl >= trail_activation:
                    trail_from = highest * (1 - trail_pct / 100)
                    if price <= trail_from:
                        sell_usd = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                        live_tx = await _live_sell(pos.ticker, sell_usd)
                        tx_tag = f" [LIVE:{live_tx.get('order_id', '')[:12]}]" if live_tx and live_tx.get("success") else ""

                        trade = CryptoPaperTrade(
                            position_id=pos.id, action="SELL",
                            pct_of_position=remaining, price=Decimal(str(price)),
                            usd_value=Decimal(str(round(sell_usd, 2))),
                            pnl_pct=Decimal(str(round(pnl_pct, 4))),
                            reason=f"Trailing stop (peak +{highest_pnl:.1f}%, now +{pnl_pct:.1f}%){tx_tag}"[:100],
                        )
                        db.add(trade)
                        pos.remaining_pct = Decimal("0")
                        pos.status = "CLOSED"
                        pos.close_reason = f"Trailing stop (+{pnl_pct:.1f}% from +{highest_pnl:.1f}% peak)"[:50]
                        pos.closed_at = datetime.utcnow()

                        if pnl_pct < 0:
                            loss_usd = abs(float(pos.position_size_usd) * (remaining / 100) * (pnl_pct / 100))
                            await _track_daily_loss(loss_usd)

                        mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                        _log.info(f"{mode} SELL (TRAIL): ${pos.ticker} @ +{pnl_pct:.1f}% (peak +{highest_pnl:.1f}%)")
                        await _send_telegram(
                            f"*{mode} TRAILING STOP: ${pos.ticker}*\n"
                            f"Peak: +{highest_pnl:.1f}%\n"
                            f"Sold @ +{pnl_pct:.1f}%\n"
                            f"Value: ${sell_usd:.2f}"
                        )
                        continue  # Position closed

            # === CHECK MAX HOLDING TIME ===
            if age_hours >= max_hold and remaining > 0:
                sell_usd = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                live_tx = await _live_sell(pos.ticker, sell_usd)
                tx_tag = f" [LIVE:{live_tx.get('order_id', '')[:12]}]" if live_tx and live_tx.get("success") else ""

                trade = CryptoPaperTrade(
                    position_id=pos.id, action="SELL",
                    pct_of_position=remaining, price=Decimal(str(price)),
                    usd_value=Decimal(str(round(sell_usd, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=f"Max hold time ({max_hold}h) @ {pnl_pct:.1f}%{tx_tag}"[:100],
                )
                db.add(trade)
                pos.remaining_pct = Decimal("0")
                pos.status = "CLOSED"
                pos.close_reason = f"Max hold time ({pnl_pct:.1f}%)"[:50]
                pos.closed_at = datetime.utcnow()

                if pnl_pct < 0:
                    loss_usd = abs(float(pos.position_size_usd) * (remaining / 100) * (pnl_pct / 100))
                    await _track_daily_loss(loss_usd)

                mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                _log.info(f"{mode} SELL (MAX HOLD): ${pos.ticker} @ {pnl_pct:.1f}% after {age_hours:.1f}h")
                await _send_telegram(
                    f"*{mode} MAX HOLD: ${pos.ticker}*\n"
                    f"Held {age_hours:.1f}h (max {max_hold}h)\n"
                    f"Sold @ {pnl_pct:.1f}%\n"
                    f"Value: ${sell_usd:.2f}"
                )
                continue  # Position closed

            # Close position if fully sold
            if float(pos.remaining_pct) <= 0 and pos.status == "OPEN":
                pos.status = "CLOSED"
                pos.closed_at = datetime.utcnow()
                if not pos.close_reason:
                    pos.close_reason = "Fully sold via take profits"

            await asyncio.sleep(0.5)

        await db.commit()


# === MAIN LOOP ===

_last_regime_update = 0.0


async def run():
    global _last_regime_update
    _log.info("Agiotage Crypto Trading Bot v2 starting")
    await asyncio.sleep(65)

    config = await get_config()
    _log.info(
        f"Config: confidence>={config['min_confidence']}, "
        f"tier2=${config['position_usd_tier2']}, tier3=${config['position_usd_tier3']}, "
        f"paper=${config['position_usd_paper']}, max={config['max_open_positions']} positions, "
        f"SL_large={config['stop_loss_large']}%, SL_mid={config['stop_loss_midcap']}%, "
        f"daily_limit=${config['daily_loss_limit_usd']}, "
        f"TP_large={len(config['tp_levels_large'])} tiers, TP_mid={len(config['tp_levels_midcap'])} tiers"
    )

    # Initial regime update
    regime = await _update_market_regime()
    _log.info(f"Initial market regime: {regime}")
    _last_regime_update = asyncio.get_event_loop().time()

    while True:
        try:
            # Update market regime every 5 minutes
            now = asyncio.get_event_loop().time()
            if now - _last_regime_update >= REGIME_UPDATE_INTERVAL:
                await _update_market_regime()
                _last_regime_update = now

            await _check_for_entries()
            await _manage_positions()
        except Exception as e:
            _log.error(f"Crypto trading bot error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Stock Trading Bot v2 — score-weighted position sizing, cap-size-aware TP/SL,
insider transaction filtering, options flow scoring, dark pool analysis,
earnings protection, breakeven stops, stop tightening, daily loss limits,
Alpaca live trading, Telegram alerts.
All parameters adjustable via API without redeploying.
"""
import asyncio
import logging
import os
import json as _json
import time
from datetime import datetime, date, timedelta
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


# === LARGE CAP DETECTION ===

LARGE_CAPS = {
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK", "JPM", "V",
    "JNJ", "WMT", "PG", "MA", "UNH", "HD", "KO", "PEP", "BAC", "ABBV", "MRK",
    "COST", "AVGO", "TMO", "CSCO", "ACN", "ABT", "MCD", "DHR", "TXN", "NEE", "LIN",
    "PM", "LOW", "UPS", "RTX", "INTC", "QCOM", "HON", "AMAT", "INTU", "ISRG",
    "BKNG", "MDLZ", "SBUX", "GILD", "GS", "BLK", "ADI", "ADP", "VRTX", "REGN",
    "SCHW", "LRCX", "MMM", "CI", "CME", "PYPL", "MRNA", "SYK", "ZTS", "SNPS",
    "KLAC", "PANW", "ORLY", "FTNT", "CDNS", "NXPI", "ADSK", "MELI", "ROP", "DXCM",
    "MNST", "AEP", "CTSH", "BIIB", "IDXX", "EXC", "WEC", "DLTR", "EA", "XEL",
    "ODFL", "FAST", "VRSK", "CSGP", "GFS", "CPRT", "CEG", "AZN", "GEHC", "CHTR",
    "ABNB", "DASH", "DDOG", "CRWD", "WDAY", "ZS", "TEAM", "SNOW", "NET", "HUBS",
    "TTD", "BILL", "OKTA", "MDB", "DKNG", "U", "COIN", "SHOP", "SQ", "SOFI",
    "PLTR", "RBLX", "PINS",
}


def _is_large_cap(ticker: str) -> bool:
    return ticker.upper() in LARGE_CAPS


# === DEFAULT CONFIG (adjustable via Redis) ===
DEFAULT_CONFIG = {
    "min_agiotage_score": 45,
    "min_sources": 2,
    "require_insider_or_congress": True,

    # Position sizing (score-weighted, $1000 account)
    "position_usd_score_45": 80,
    "position_usd_score_55": 125,
    "position_usd_score_65": 180,
    "max_open_positions": 5,
    "max_per_sector": 2,
    "cash_reserve_pct": 35,
    "daily_loss_limit_usd": 75,

    # Insider filters
    "min_insider_value": 300000,
    "filter_awards_exercises": True,
    "csuite_multiplier": 2.0,

    # TP/SL -- Large cap
    "tp_levels_large": [
        {"sell_pct": 25, "at_profit_pct": 5},
        {"sell_pct": 25, "at_profit_pct": 12},
        {"sell_pct": 25, "at_profit_pct": 20},
        {"sell_pct": 25, "at_profit_pct": 35},
    ],
    "stop_loss_large": 7,
    "trailing_activation_large": 15,
    "trailing_trail_large": 5,
    "max_holding_days_large": 45,

    # TP/SL -- Small/mid cap
    "tp_levels_smid": [
        {"sell_pct": 20, "at_profit_pct": 8},
        {"sell_pct": 20, "at_profit_pct": 18},
        {"sell_pct": 30, "at_profit_pct": 28},
        {"sell_pct": 30, "at_profit_pct": 50},
    ],
    "stop_loss_smid": 8,
    "trailing_activation_smid": 20,
    "trailing_trail_smid": 7,
    "max_holding_days_smid": 60,

    # Options-sourced max hold
    "max_holding_days_options": 14,

    # Breakeven stop
    "breakeven_stop_after_first_tp": True,

    # Stop tightening
    "tighten_sl_after_pct_large": 15,
    "tighten_sl_to_large": 4,
    "tighten_sl_after_pct_smid": 20,
    "tighten_sl_to_smid": 3,

    # Earnings protection
    "earnings_blackout_days": 5,
    "auto_exit_before_earnings_days": 2,

    # Options flow scoring
    "call_sweep_250k_score": 8,
    "call_sweep_500k_score": 12,
    "put_sweep_block_threshold": 250000,
    "insider_options_multiplier": 1.3,

    # Dark pool
    "darkpool_buy_threshold": 2000000,
    "darkpool_buy_score": 7,
    "darkpool_sell_block": 3000000,

    # Negative blockers
    "block_if_insider_sells_gt_buys": True,
    "block_if_put_sweep_bearish": True,
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


# === DAILY LOSS TRACKING ===

async def _track_daily_loss(loss_usd: float):
    try:
        from ..core.redis import redis_client
        key = f"stock_trader:daily_loss:{datetime.utcnow().strftime('%Y-%m-%d')}"
        current = float(await redis_client.get(key) or 0)
        await redis_client.set(key, str(current + loss_usd), ex=86400)
    except:
        pass


async def _get_daily_loss() -> float:
    try:
        from ..core.redis import redis_client
        key = f"stock_trader:daily_loss:{datetime.utcnow().strftime('%Y-%m-%d')}"
        return float(await redis_client.get(key) or 0)
    except:
        return 9999  # Redis down = assume limit hit, block new entries


# === MARKET HOURS CHECK ===

def _is_market_hours() -> bool:
    """Check if US stock market is currently open (9:30 AM - 4:00 PM ET, weekdays)."""
    try:
        import pytz
        et = pytz.timezone("US/Eastern")
        now = datetime.now(et)
    except ImportError:
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
    """Get current stock price from Yahoo Finance."""
    try:
        async with httpx.AsyncClient() as client:
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


# === EARNINGS CHECK ===

async def _check_earnings_near(ticker: str, days: int = 5) -> bool:
    """Check if earnings are within N days. Uses Finnhub free API."""
    try:
        today = date.today()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://finnhub.io/api/v1/calendar/earnings",
                params={"symbol": ticker, "from": today.isoformat(),
                        "to": (today + timedelta(days=days)).isoformat(),
                        "token": os.getenv("FINNHUB_API_KEY", "")},
                timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                return len(data.get("earningsCalendar", [])) > 0
    except:
        pass
    return False


# === LIVE TRADING HELPERS (Alpaca) ===

async def _is_live_mode() -> bool:
    try:
        from ..core.redis import redis_client
        paused = await redis_client.get("trading:paused")
        if paused == "1":
            return False
        tc = await redis_client.get("trading:config")
        if tc:
            tc_data = _json.loads(tc)
            return tc_data.get("live_mode", False) and "stock_trader" in tc_data.get("allowed_traders", [])
    except:
        pass
    return False


async def _live_buy(symbol: str, amount_usd: float) -> dict | None:
    """Execute a live buy via Alpaca if live mode is on. Returns tx result or None."""
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
    """Execute a live sell via Alpaca if live mode is on. Returns tx result or None."""
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


async def _live_sell_all(symbol: str) -> dict | None:
    """Close entire position via Alpaca if live mode is on."""
    if not await _is_live_mode():
        return None
    try:
        from ..services.kraken_exchange import sell_all
        result = await sell_all(symbol)
        if result.get("success"):
            _log.info(f"LIVE SELL ALL: ${symbol} order={result.get('order_id')}")
        else:
            _log.error(f"LIVE SELL ALL FAILED: ${symbol} -- {result.get('error')}")
        return result
    except Exception as e:
        _log.error(f"LIVE SELL ALL ERROR: ${symbol} -- {e}")
        return None


# === POSITION SIZING BY SCORE ===

def _position_usd_for_score(score: int, config: dict) -> float:
    """Return position size in USD based on agiotage score."""
    if score >= 65:
        return config["position_usd_score_65"]
    elif score >= 55:
        return config["position_usd_score_55"]
    else:
        return config["position_usd_score_45"]


# === CAP-SIZE CONFIG HELPERS ===

def _get_tp_levels(ticker: str, config: dict) -> list:
    """Return take-profit levels based on cap size."""
    if _is_large_cap(ticker):
        return config.get("tp_levels_large", [])
    return config.get("tp_levels_smid", [])


def _get_stop_loss_pct(ticker: str, config: dict) -> float:
    """Return stop loss percentage based on cap size."""
    if _is_large_cap(ticker):
        return config.get("stop_loss_large", 7)
    return config.get("stop_loss_smid", 8)


def _get_trailing_config(ticker: str, config: dict) -> tuple:
    """Return (activation_pct, trail_pct) based on cap size."""
    if _is_large_cap(ticker):
        return config.get("trailing_activation_large", 15), config.get("trailing_trail_large", 5)
    return config.get("trailing_activation_smid", 20), config.get("trailing_trail_smid", 7)


def _get_max_holding_days(ticker: str, sources_str: str, config: dict) -> int:
    """Return max holding days. Options-sourced positions have shorter hold."""
    if "options" in (sources_str or "").lower():
        return config.get("max_holding_days_options", 14)
    if _is_large_cap(ticker):
        return config.get("max_holding_days_large", 45)
    return config.get("max_holding_days_smid", 60)


def _get_tighten_sl_config(ticker: str, config: dict) -> tuple:
    """Return (tighten_after_pct, tighten_to_pct) based on cap size."""
    if _is_large_cap(ticker):
        return config.get("tighten_sl_after_pct_large", 15), config.get("tighten_sl_to_large", 4)
    return config.get("tighten_sl_after_pct_smid", 20), config.get("tighten_sl_to_smid", 3)


# === SIGNAL DEDUP ===

_recent_entries: dict[str, float] = {}  # ticker -> timestamp of last entry


def _is_dedup_blocked(ticker: str) -> bool:
    """Check if ticker had an entry in the last 5 minutes."""
    last = _recent_entries.get(ticker, 0)
    return (time.time() - last) < 300  # 5 min cooldown


def _mark_entry(ticker: str):
    """Mark that we opened a position for this ticker."""
    _recent_entries[ticker] = time.time()


# === ENTRY LOGIC ===

async def _check_for_entries():
    if not _is_market_hours():
        return

    config = await get_config()

    # Check daily loss limit
    daily_loss = await _get_daily_loss()
    if daily_loss >= config["daily_loss_limit_usd"]:
        _log.warning(f"Daily loss limit reached: ${daily_loss:.2f} >= ${config['daily_loss_limit_usd']}")
        return

    async with async_session() as db:
        # Count open positions
        open_count = (await db.execute(
            select(func.count()).select_from(StockPaperPosition)
            .where(StockPaperPosition.status == "OPEN")
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

            # Signal dedup (5 min cooldown per ticker)
            if _is_dedup_blocked(ticker):
                continue

            # Check if we already have a position in this ticker
            existing = (await db.execute(
                select(StockPaperPosition)
                .where(StockPaperPosition.ticker == ticker,
                       StockPaperPosition.status == "OPEN")
            )).scalar_one_or_none()
            if existing:
                continue

            # === INSIDER TRANSACTION FILTERING ===
            # Filter out Awards (A), Exercises (M), Gifts (G) -- only count Purchase (P)
            insider_buys = (await db.execute(
                select(func.count()).select_from(StockWhaleMove)
                .where(StockWhaleMove.ticker == ticker,
                       StockWhaleMove.source == "insider",
                       StockWhaleMove.action == "Purchase",
                       StockWhaleMove.created_at >= cutoff_7d)
            )).scalar() or 0

            insider_sells = (await db.execute(
                select(func.count()).select_from(StockWhaleMove)
                .where(StockWhaleMove.ticker == ticker,
                       StockWhaleMove.source == "insider",
                       StockWhaleMove.action == "Sale",
                       StockWhaleMove.created_at >= cutoff_7d)
            )).scalar() or 0

            # HARD BLOCKER: insider sells > buys
            if config["block_if_insider_sells_gt_buys"] and insider_sells > insider_buys:
                _log.info(f"BLOCK ${ticker}: insider sells ({insider_sells}) > buys ({insider_buys})")
                continue

            # Count congress BUYS
            congress_buys = (await db.execute(
                select(func.count()).select_from(StockWhaleMove)
                .where(StockWhaleMove.ticker == ticker,
                       StockWhaleMove.source == "congress",
                       StockWhaleMove.action == "Purchase",
                       StockWhaleMove.created_at >= cutoff_7d)
            )).scalar() or 0

            # === CHECK EARNINGS BLACKOUT ===
            earnings_near = await _check_earnings_near(ticker, config.get("earnings_blackout_days", 5))
            if earnings_near:
                _log.info(f"SKIP ${ticker}: earnings within {config.get('earnings_blackout_days', 5)} days")
                continue

            # === SCORE CALCULATION ===
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

            # Insider buys -- value-weighted
            if insider_buys > 0:
                sources.append("insider")

                # Get total insider buy value
                insider_total_value = (await db.execute(
                    select(func.sum(StockWhaleMove.value_usd))
                    .where(StockWhaleMove.ticker == ticker,
                           StockWhaleMove.source == "insider",
                           StockWhaleMove.action == "Purchase",
                           StockWhaleMove.created_at >= cutoff_7d)
                )).scalar() or 0
                insider_total_value = float(insider_total_value)

                # Check min_insider_value
                if insider_total_value < config["min_insider_value"]:
                    sources.remove("insider")
                else:
                    # Value-weighted scoring per council decision
                    if insider_total_value >= 1_000_000:
                        score += 12
                    elif insider_total_value >= 500_000:
                        score += 8
                    else:
                        score += 3

                    # Multiple insider buys bonus
                    score += min(insider_buys * 3, 12)

                    # C-suite multiplier: check if any filer is C-suite
                    csuite_filers = (await db.execute(
                        select(StockWhaleMove.filer_title)
                        .where(StockWhaleMove.ticker == ticker,
                               StockWhaleMove.source == "insider",
                               StockWhaleMove.action == "Purchase",
                               StockWhaleMove.created_at >= cutoff_7d)
                        .limit(10)
                    )).scalars().all()

                    csuite_keywords = {"ceo", "cfo", "coo", "cto", "president", "chairman",
                                       "chief executive", "chief financial", "chief operating",
                                       "chief technology", "director"}
                    has_csuite = False
                    for title in csuite_filers:
                        if title and any(kw in title.lower() for kw in csuite_keywords):
                            has_csuite = True
                            break

                    if has_csuite:
                        # Apply C-suite multiplier ONLY to insider-specific score
                        # Track what insider added: value score (3/8/12) + per-buy bonus
                        insider_only_score = 0
                        if insider_total_value >= 1_000_000:
                            insider_only_score = 12
                        elif insider_total_value >= 500_000:
                            insider_only_score = 8
                        else:
                            insider_only_score = 3
                        insider_only_score += min(insider_buys * 3, 12)
                        bonus = int(insider_only_score * (config["csuite_multiplier"] - 1))
                        score += bonus
                        sources.append("csuite")

            # Congress buys -- clustering per council decision
            if congress_buys > 0:
                sources.append("congress")
                if congress_buys >= 4:
                    score += 20
                elif congress_buys >= 2:
                    score += 12
                else:
                    score += 4

            # Options flow scoring
            options_score = 0
            if signal.sources and "options" in signal.sources.lower():
                sources.append("options")

                # Check for call sweep data in whale moves
                call_sweeps = (await db.execute(
                    select(StockWhaleMove)
                    .where(StockWhaleMove.ticker == ticker,
                           StockWhaleMove.source == "options",
                           StockWhaleMove.created_at >= cutoff_7d)
                    .limit(10)
                )).scalars().all()

                for sweep in call_sweeps:
                    sweep_value = float(sweep.value_usd or 0)
                    sweep_action = (sweep.action or "").lower()

                    # Call sweeps
                    if "call" in sweep_action or "call" in (sweep.filer_name or "").lower():
                        if sweep_value >= 500_000:
                            options_score += config["call_sweep_500k_score"]
                        elif sweep_value >= 250_000:
                            options_score += config["call_sweep_250k_score"]

                    # HARD BLOCKER: put sweeps
                    if "put" in sweep_action or "put" in (sweep.filer_name or "").lower():
                        if sweep_value >= config["put_sweep_block_threshold"] and config["block_if_put_sweep_bearish"]:
                            _log.info(f"BLOCK ${ticker}: bearish put sweep ${sweep_value:,.0f}")
                            options_score = -999  # Signal to block
                            break

                if options_score == -999:
                    continue  # Blocked by put sweep

                score += options_score

            # Dark pool scoring
            if signal.sources and "darkpool" in signal.sources.lower():
                dark_moves = (await db.execute(
                    select(StockWhaleMove)
                    .where(StockWhaleMove.ticker == ticker,
                           StockWhaleMove.source == "darkpool",
                           StockWhaleMove.created_at >= cutoff_7d)
                    .limit(10)
                )).scalars().all()

                dark_buy_total = 0
                dark_sell_total = 0
                for dm in dark_moves:
                    dm_value = float(dm.value_usd or 0)
                    if (dm.action or "").lower() in ("purchase", "buy"):
                        dark_buy_total += dm_value
                    elif (dm.action or "").lower() in ("sale", "sell"):
                        dark_sell_total += dm_value

                # HARD BLOCKER: dark pool net sells > threshold
                if dark_sell_total >= config["darkpool_sell_block"]:
                    _log.info(f"BLOCK ${ticker}: dark pool net sells ${dark_sell_total:,.0f}")
                    continue

                if dark_buy_total >= config["darkpool_buy_threshold"]:
                    score += config["darkpool_buy_score"]
                    sources.append("darkpool")

            # 13f source
            has_13f = (await db.execute(
                select(func.count()).select_from(StockWhaleMove)
                .where(StockWhaleMove.ticker == ticker,
                       StockWhaleMove.source == "13f",
                       StockWhaleMove.created_at >= cutoff_7d)
            )).scalar() or 0
            if has_13f > 0:
                sources.append("13f")

            # Social sentiment
            try:
                sent_count = (await db.execute(
                    select(func.count()).select_from(SocialMention)
                    .where(SocialMention.token_symbol == ticker,
                           SocialMention.detected_at >= cutoff_24h)
                )).scalar() or 0
                if sent_count >= 3:
                    score += 10
                    sources.append("social")
            except:
                pass

            # Insider + options combo multiplier
            if "insider" in sources and "options" in sources:
                score = int(score * config["insider_options_multiplier"])

            # === ENTRY CRITERIA ===
            if score < config["min_agiotage_score"]:
                continue
            if len(sources) < config["min_sources"]:
                continue

            # Must include insider or congress
            if config["require_insider_or_congress"]:
                if "insider" not in sources and "congress" not in sources:
                    continue

            # Max per sector check (use sector from signal if available)
            # Simple approach: count open positions with same ticker prefix won't work.
            # Skip sector check if not available in signal data.

            # Re-check open positions (may have opened one this loop)
            open_count = (await db.execute(
                select(func.count()).select_from(StockPaperPosition)
                .where(StockPaperPosition.status == "OPEN")
            )).scalar() or 0
            if open_count >= config["max_open_positions"]:
                return

            # Get current price
            price = await _get_stock_price(ticker)
            if price <= 0:
                continue

            # Position size by score
            position_usd = _position_usd_for_score(score, config)

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

            # === LIVE BUY (Alpaca) ===
            live_result = None
            live_mode = await _is_live_mode()

            if live_mode:
                # Re-check kill switch right before execution
                try:
                    from ..core.redis import redis_client
                    if (await redis_client.get("trading:paused")) == "1":
                        live_mode = False
                except:
                    live_mode = False

            if live_mode:
                live_result = await _live_buy(ticker, position_usd)
                if live_result and not live_result.get("success"):
                    # Don't create position if live buy fails
                    _log.error(f"SKIP ${ticker}: live buy failed, not creating position")
                    continue
                elif not live_result:
                    live_mode = False

            # === RECORD POSITION AND TRADE ===
            live_tag = ""
            if live_result and live_result.get("success"):
                order_id = live_result.get("order_id", "")
                live_tag = f" [LIVE:{order_id[:12]}]" if order_id else " [LIVE]"

            cap_label = "large" if _is_large_cap(ticker) else "smid"

            position = StockPaperPosition(
                ticker=ticker,
                company_name=None,
                entry_price=Decimal(str(price)),
                position_size_usd=Decimal(str(position_usd)),
                current_price=Decimal(str(price)),
                highest_price=Decimal(str(price)),
                pnl_pct=Decimal("0"),
                pnl_usd=Decimal("0"),
                entry_signal=sig_type,
                entry_sources=",".join(sources),
                agiotage_score=score,
            )
            db.add(position)

            reason = f"Signal: {sig_type}, Score: {score}, {cap_label}{live_tag}"
            trade = StockPaperTrade(
                position_id=0,  # Updated after commit
                action="BUY",
                pct_of_position=100,
                price=Decimal(str(price)),
                usd_value=Decimal(str(position_usd)),
                pnl_pct=Decimal("0"),
                reason=reason[:100],
            )

            await db.commit()
            await db.refresh(position)
            trade.position_id = position.id
            db.add(trade)
            await db.commit()

            _mark_entry(ticker)

            mode_label = "LIVE" if live_result and live_result.get("success") else "PAPER"
            _log.info(f"{mode_label} BUY: ${ticker} @ ${price:.2f} Score={score} "
                       f"Size=${position_usd:.2f} Cap={cap_label} [{','.join(sources)}]")

            # Telegram alert
            filers_str = ", ".join(insider_names[:3]) if insider_names else "N/A"
            msg = (
                f"{'$' if mode_label == 'LIVE' else '#'} *{mode_label} BUY: ${ticker}*\n\n"
                f"Price: ${price:.2f}\n"
                f"Size: ${position_usd:.2f}\n"
                f"Score: {score}\n"
                f"Cap: {cap_label}\n"
                f"Sources: {', '.join(sources)}\n"
                f"Filers: {filers_str}"
            )
            await _send_telegram(msg)


# === POSITION MANAGEMENT ===

async def _manage_positions():
    if not _is_market_hours():
        return

    config = await get_config()

    async with async_session() as db:
        positions = (await db.execute(
            select(StockPaperPosition).where(StockPaperPosition.status == "OPEN")
        )).scalars().all()

        for pos in positions:
            price = await _get_stock_price(pos.ticker)
            if price <= 0:
                continue

            entry = float(pos.entry_price)
            if entry <= 0:
                continue

            pnl_pct = ((price - entry) / entry) * 100
            pnl_usd = (pnl_pct / 100) * float(pos.position_size_usd) * (float(pos.remaining_pct) / 100)
            highest = max(float(pos.highest_price or price), price)

            pos.current_price = Decimal(str(price))
            pos.highest_price = Decimal(str(highest))
            pos.pnl_pct = Decimal(str(round(pnl_pct, 4)))
            pos.pnl_usd = Decimal(str(round(pnl_usd, 2)))
            pos.last_updated = datetime.utcnow()

            remaining = float(pos.remaining_pct)
            age_days = (datetime.utcnow() - pos.opened_at).total_seconds() / 86400

            # Get cap-size-specific configs
            tp_levels = _get_tp_levels(pos.ticker, config)
            base_sl_pct = _get_stop_loss_pct(pos.ticker, config)
            trail_activation, trail_pct = _get_trailing_config(pos.ticker, config)
            max_hold_days = _get_max_holding_days(pos.ticker, pos.entry_sources, config)
            tighten_after_pct, tighten_to = _get_tighten_sl_config(pos.ticker, config)

            # Stop tightening: if position is up X%, tighten stop loss
            effective_sl = base_sl_pct
            if pnl_pct >= tighten_after_pct:
                effective_sl = tighten_to

            # Check if first TP was already hit (for breakeven stop)
            first_tp_hit = False
            if config["breakeven_stop_after_first_tp"] and tp_levels:
                first_tp = tp_levels[0]
                existing_first_tp = (await db.execute(
                    select(StockPaperTrade)
                    .where(StockPaperTrade.position_id == pos.id,
                           StockPaperTrade.reason.contains(f"TP {first_tp['at_profit_pct']}%"))
                )).scalar_one_or_none()
                if existing_first_tp:
                    first_tp_hit = True

            # === CHECK EARNINGS AUTO-EXIT ===
            if remaining > 0:
                earnings_imminent = await _check_earnings_near(
                    pos.ticker, config.get("auto_exit_before_earnings_days", 2))
                if earnings_imminent:
                    sell_usd = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                    live_tx = await _live_sell(pos.ticker, sell_usd)
                    tx_tag = f" [LIVE:{live_tx.get('order_id', '')[:12]}]" if live_tx and live_tx.get("success") else ""

                    trade = StockPaperTrade(
                        position_id=pos.id, action="SELL",
                        pct_of_position=remaining, price=Decimal(str(price)),
                        usd_value=Decimal(str(round(sell_usd, 2))),
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        reason=f"Earnings auto-exit @ {pnl_pct:.1f}%{tx_tag}"[:100],
                    )
                    db.add(trade)
                    pos.remaining_pct = Decimal("0")
                    pos.status = "CLOSED"
                    pos.close_reason = f"Earnings auto-exit ({pnl_pct:.1f}%)"[:50]
                    pos.closed_at = datetime.utcnow()

                    if pnl_pct < 0:
                        loss_usd = abs(float(pos.position_size_usd) * (remaining / 100) * (pnl_pct / 100))
                        await _track_daily_loss(loss_usd)

                    mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                    _log.info(f"{mode} SELL (EARNINGS): ${pos.ticker} @ {pnl_pct:.1f}%")
                    await _send_telegram(
                        f"*{mode} EARNINGS EXIT: ${pos.ticker}*\n"
                        f"Earnings approaching\n"
                        f"Sold {remaining:.0f}% @ {pnl_pct:.1f}%\n"
                        f"Value: ${sell_usd:.2f}"
                    )
                    continue  # Position closed

            # === CHECK TAKE PROFIT LEVELS ===
            for tp in tp_levels:
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

                    sell_usd = float(pos.position_size_usd) * (sell_pct / 100) * (1 + pnl_pct / 100)
                    live_tx = await _live_sell(pos.ticker, sell_usd)
                    tx_tag = f" [LIVE:{live_tx.get('order_id', '')[:12]}]" if live_tx and live_tx.get("success") else ""

                    trade = StockPaperTrade(
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
                # After first TP hit, stop loss moves to breakeven (entry + small buffer)
                sl_triggered = pnl_pct <= 1.0  # 1% buffer above entry
                close_reason = f"Breakeven stop ({pnl_pct:.1f}%)"
            else:
                sl_triggered = pnl_pct <= -effective_sl
                close_reason = f"Stop loss ({pnl_pct:.1f}%, SL={effective_sl}%)"

            if sl_triggered and remaining > 0:
                sell_usd = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                live_tx = await _live_sell(pos.ticker, sell_usd)
                tx_tag = f" [LIVE:{live_tx.get('order_id', '')[:12]}]" if live_tx and live_tx.get("success") else ""

                trade = StockPaperTrade(
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
            if remaining > 0:
                highest_pnl = ((highest - entry) / entry) * 100
                if highest_pnl >= trail_activation:
                    trail_from = highest * (1 - trail_pct / 100)
                    if price <= trail_from:
                        sell_usd = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                        live_tx = await _live_sell(pos.ticker, sell_usd)
                        tx_tag = f" [LIVE:{live_tx.get('order_id', '')[:12]}]" if live_tx and live_tx.get("success") else ""

                        trade = StockPaperTrade(
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
            if age_days >= max_hold_days and remaining > 0:
                sell_usd = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                live_tx = await _live_sell(pos.ticker, sell_usd)
                tx_tag = f" [LIVE:{live_tx.get('order_id', '')[:12]}]" if live_tx and live_tx.get("success") else ""

                trade = StockPaperTrade(
                    position_id=pos.id, action="SELL",
                    pct_of_position=remaining, price=Decimal(str(price)),
                    usd_value=Decimal(str(round(sell_usd, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=f"Max hold ({max_hold_days}d) @ {pnl_pct:.1f}%{tx_tag}"[:100],
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
                _log.info(f"{mode} SELL (MAX HOLD): ${pos.ticker} @ {pnl_pct:.1f}% after {age_days:.0f}d")
                await _send_telegram(
                    f"*{mode} MAX HOLD: ${pos.ticker}*\n"
                    f"Held {age_days:.0f}d (max {max_hold_days}d)\n"
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

async def run():
    _log.info("Stock Trading Bot v2 starting")
    await asyncio.sleep(70)

    config = await get_config()
    _log.info(
        f"Config: score>={config['min_agiotage_score']}, "
        f"size_45=${config['position_usd_score_45']}, size_55=${config['position_usd_score_55']}, "
        f"size_65=${config['position_usd_score_65']}, max={config['max_open_positions']} positions, "
        f"SL_large={config['stop_loss_large']}%, SL_smid={config['stop_loss_smid']}%, "
        f"daily_limit=${config['daily_loss_limit_usd']}, "
        f"TP_large={len(config['tp_levels_large'])} tiers, TP_smid={len(config['tp_levels_smid'])} tiers"
    )

    last_entry_check = 0

    while True:
        try:
            now = time.time()

            # Entry logic every 30 minutes (only during market hours)
            if now - last_entry_check >= ENTRY_CHECK_INTERVAL:
                await _check_for_entries()
                last_entry_check = now

            # Position management every 5 minutes (only during market hours)
            await _manage_positions()

        except Exception as e:
            _log.error(f"Stock trading bot v2 error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

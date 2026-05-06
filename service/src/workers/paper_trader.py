# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Agiotage Meme Trading Bot v2 — score-weighted entries, 5-tier TP, breakeven stops,
GMGN security checks, deployer dump detection, daily loss limits, rapid polling.
All parameters adjustable via API without redeploying.
"""
import asyncio
import logging
import os
import json as _json
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Float, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("paper-trader")

NORMAL_POLL = 30
RAPID_POLL = 10


# === DEFAULT CONFIG (adjustable via Redis) ===
DEFAULT_CONFIG = {
    # Entry criteria
    "min_agiotage_score": 45,
    "min_mc": 100000,
    "max_mc": 500000,
    "min_sources": 2,
    "min_wallet_count": 5,
    "min_wallet_count_with_deployer": 3,
    "max_price_move_pct": 40,
    "signal_lookback_minutes": 5,

    # Position sizing (score-weighted)
    "base_position_sol": 0.08,
    "position_sol_score_45": 0.05,
    "position_sol_score_55": 0.08,
    "position_sol_score_65": 0.12,
    "max_open_positions": 4,
    "max_position_pct_of_pool": 1.0,
    "daily_loss_limit_sol": 0.15,

    # Take profit (5-tier)
    "take_profit_levels": [
        {"sell_pct": 25, "at_profit_pct": 25},
        {"sell_pct": 20, "at_profit_pct": 45},
        {"sell_pct": 25, "at_profit_pct": 80},
        {"sell_pct": 20, "at_profit_pct": 150},
        {"sell_pct": 10, "at_profit_pct": 300},
    ],

    # Stop loss & trailing
    "stop_loss_pct": 25,
    "trailing_stop_enabled": True,
    "trailing_stop_activation_pct": 20,
    "trailing_stop_trail_pct": 10,
    "breakeven_stop_after_first_tp": True,
    "max_holding_hours": 12,

    # Execution
    "buy_slippage_bps": 200,
    "sell_slippage_bps": 300,
    "panic_slippage_bps": 800,
    "priority_fee_lamports": 50000,
    "rapid_poll_threshold_pct": 5,

    # Security filters
    "require_security_check": True,
    "max_sell_tax": 0.10,
    "min_liquidity_to_mc_ratio": 0.10,
    "block_deployer_d": True,
    "bot_saturation_threshold": 20,

    # Filters
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


# === DAILY LOSS TRACKING ===

async def _track_daily_loss(loss_sol: float):
    try:
        from ..core.redis import redis_client
        key = f"paper_trader:daily_loss:{datetime.utcnow().strftime('%Y-%m-%d')}"
        current = float(await redis_client.get(key) or 0)
        await redis_client.set(key, str(current + loss_sol), ex=86400)
    except:
        pass


async def _get_daily_loss() -> float:
    try:
        from ..core.redis import redis_client
        key = f"paper_trader:daily_loss:{datetime.utcnow().strftime('%Y-%m-%d')}"
        return float(await redis_client.get(key) or 0)
    except:
        return 0


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


async def _get_price_mc_liquidity(token_addr: str) -> tuple:
    """Get price, MC, and liquidity USD from DexScreener."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{token_addr}", timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if pairs:
                    pair = pairs[0]
                    price = float(pair.get("priceUsd", 0) or 0)
                    mc = float(pair.get("fdv", 0) or 0)
                    liq = float(pair.get("liquidity", {}).get("usd", 0) or 0) if isinstance(pair.get("liquidity"), dict) else 0
                    return price, mc, liq
    except:
        pass
    return 0, 0, 0


async def _get_sol_price() -> float:
    """Fetch current SOL/USD price."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", timeout=5)
            if resp.status_code == 200:
                return resp.json().get("solana", {}).get("usd", 150)
    except:
        pass
    return 150


# === GMGN SECURITY CHECK ===

async def _check_token_security(token_address: str) -> dict:
    """Check GMGN token security. Returns {safe: bool, reasons: [str]}"""
    api_key = os.getenv("GMGN_API_KEY", "")
    if not api_key:
        return {"safe": True, "reasons": []}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://openapi.gmgn.ai/v1/token/security",
                params={"chain": "sol", "address": token_address,
                        "timestamp": int(time.time()), "client_id": str(uuid.uuid4())},
                headers={"X-APIKEY": api_key}, timeout=8)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                reasons = []
                if data.get("is_honeypot") == "yes":
                    reasons.append("honeypot")
                if float(data.get("sell_tax", 0) or 0) > 0.10:
                    reasons.append(f"sell_tax={data['sell_tax']}")
                if float(data.get("buy_tax", 0) or 0) > 0.10:
                    reasons.append(f"buy_tax={data['buy_tax']}")
                return {"safe": len(reasons) == 0, "reasons": reasons}
    except:
        pass
    return {"safe": True, "reasons": []}


# === LIVE TRADING HELPERS ===

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


async def _live_sell(token_address: str, sell_pct: float, reason: str, slippage_bps: int = 300) -> dict | None:
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
        result = await sell_token(token_address, sell_amount, decimals, slippage_bps=slippage_bps)
        if result.get("success"):
            _log.info(f"LIVE SELL: {reason} tx={result.get('tx_hash')}")
        else:
            _log.error(f"LIVE SELL FAILED: {reason} -- {result.get('error')}")
        return result
    except Exception as e:
        _log.error(f"LIVE SELL ERROR: {e}")
        return None


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


# === POSITION SIZE BY SCORE ===

def _position_sol_for_score(score: int, config: dict) -> float:
    """Return position size in SOL based on agiotage score."""
    if score >= 65:
        return config["position_sol_score_65"]
    elif score >= 55:
        return config["position_sol_score_55"]
    else:
        return config["position_sol_score_45"]


# === ENTRY LOGIC ===

async def _check_for_entries():
    config = await get_config()

    async with async_session() as db:
        # 1. Count open positions
        open_count = (await db.execute(
            select(func.count()).select_from(PaperPosition).where(PaperPosition.status == "OPEN")
        )).scalar() or 0

        if open_count >= config["max_open_positions"]:
            return

        # 2. Check daily loss limit
        daily_loss = await _get_daily_loss()
        if daily_loss >= config["daily_loss_limit_sol"]:
            _log.warning(f"Daily loss limit reached: {daily_loss:.4f} SOL >= {config['daily_loss_limit_sol']} SOL")
            return

        # 3. Get cluster signals from last N minutes
        from .smart_money_tracker import ClusterSignal
        lookback = config.get("signal_lookback_minutes", 5)
        cutoff = datetime.utcnow() - timedelta(minutes=lookback)

        signals = (await db.execute(
            select(ClusterSignal)
            .where(ClusterSignal.detected_at >= cutoff)
            .order_by(ClusterSignal.detected_at.desc())
            .limit(10)
        )).scalars().all()

        for signal in signals:
            symbol = (signal.token_symbol or "").upper()

            # 4a. Skip if symbol in skip_symbols
            if symbol in config["skip_symbols"]:
                continue

            # 4b. Skip if already have open or recently opened position for this token (dedup)
            existing = (await db.execute(
                select(PaperPosition)
                .where(PaperPosition.token_address == signal.token_address,
                       PaperPosition.status.in_(["OPEN"]))
            )).scalar_one_or_none()
            if existing:
                continue
            # Also check if we opened a position for this token in the last 5 minutes (prevents double-buy race)
            recent_entry = (await db.execute(
                select(PaperPosition)
                .where(PaperPosition.token_address == signal.token_address,
                       PaperPosition.opened_at >= datetime.utcnow() - timedelta(minutes=5))
            )).scalar_one_or_none()
            if recent_entry:
                continue

            # 4c. Get current price, MC, and liquidity from DexScreener
            price, mc, liquidity = await _get_price_mc_liquidity(signal.token_address)
            if price <= 0:
                continue

            # 4d. Skip if MC out of range
            if mc < config["min_mc"] or mc > config["max_mc"]:
                continue

            # 4e. Check price move since signal
            if signal.price_at_signal and float(signal.price_at_signal) > 0:
                price_at = float(signal.price_at_signal)
                move_pct = ((price - price_at) / price_at) * 100
                if move_pct > config["max_price_move_pct"]:
                    _log.info(f"SKIP ${symbol}: price moved +{move_pct:.1f}% since signal (max {config['max_price_move_pct']}%)")
                    continue

            # 4f. GMGN security check
            if config["require_security_check"]:
                sec = await _check_token_security(signal.token_address)
                if not sec["safe"]:
                    _log.info(f"SKIP ${symbol}: security check failed: {', '.join(sec['reasons'])}")
                    continue

            # 4g. Liquidity-to-MC ratio check
            if mc > 0 and liquidity > 0:
                liq_ratio = liquidity / mc
                if liq_ratio < config["min_liquidity_to_mc_ratio"]:
                    _log.info(f"SKIP ${symbol}: liquidity/MC ratio {liq_ratio:.3f} < {config['min_liquidity_to_mc_ratio']}")
                    continue
            elif mc > 0 and liquidity <= 0:
                # No liquidity data available -- skip if security check required
                if config["require_security_check"]:
                    _log.info(f"SKIP ${symbol}: no liquidity data")
                    continue

            # 4h. Score calculation
            score = 0
            sources = []

            # Smart money signal strength
            cl_boost = {"VERY_STRONG": 30, "STRONG": 20, "MEDIUM": 15}.get(signal.signal_strength, 10)
            score += cl_boost
            sources.append("smart_money")

            # Wallet winrate bonus
            if signal.avg_wallet_winrate and float(signal.avg_wallet_winrate) > 0.55:
                score += 10

            # Deployer token scoring
            deployer_rating = None
            if signal.is_deployer_token:
                try:
                    from ..models.platform import TopDeployer
                    from .smart_money_tracker import TokenDeployment
                    td = (await db.execute(
                        select(TopDeployer).join(
                            TokenDeployment,
                            TokenDeployment.deployer_wallet == TopDeployer.wallet
                        ).where(TokenDeployment.token_address == signal.token_address)
                    )).scalar_one_or_none()
                    if td:
                        deployer_rating = td.rating
                        if deployer_rating in ("S", "A"):
                            score += 15
                            sources.append("deployer_SA")
                        elif deployer_rating == "B":
                            score += 5
                            sources.append("deployer_B")
                        elif deployer_rating == "D" and config["block_deployer_d"]:
                            score -= 50
                            sources.append("deployer_D_BLOCKED")
                    else:
                        # Deployer token but no rating found -- mild bonus
                        score += 5
                        sources.append("deployer")
                except Exception:
                    score += 5
                    sources.append("deployer")

            # Sentiment check (5+ mentions)
            try:
                from .sentiment_tracker import SocialMention
                sent_count = (await db.execute(
                    select(func.count()).select_from(SocialMention)
                    .where(SocialMention.token_symbol == symbol,
                           SocialMention.detected_at >= datetime.utcnow() - timedelta(hours=6))
                )).scalar() or 0
                if sent_count >= 5:
                    score += 10
                    sources.append("social")
            except Exception:
                pass

            # Followed wallet signals for same token
            try:
                from .wallet_follow import FollowedWalletTrade
                fw_count = (await db.execute(
                    select(func.count()).select_from(FollowedWalletTrade)
                    .where(FollowedWalletTrade.token_address == signal.token_address,
                           FollowedWalletTrade.side == "buy",
                           FollowedWalletTrade.trade_time >= datetime.utcnow() - timedelta(hours=6))
                )).scalar() or 0
                if fw_count > 0:
                    score += 20
                    sources.append("followed_wallets")
            except Exception:
                pass

            # LunarCrush Galaxy Score (if available)
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
                                score += 15
                                sources.append("lunarcrush")
            except Exception:
                pass

            # 4i. Wallet count gate: 5+ wallets, OR 3+ with S/A deployer
            wallet_count = signal.wallet_count or 0
            if deployer_rating in ("S", "A"):
                if wallet_count < config["min_wallet_count_with_deployer"]:
                    _log.info(f"SKIP ${symbol}: {wallet_count} wallets < {config['min_wallet_count_with_deployer']} (deployer gate)")
                    continue
            else:
                if wallet_count < config["min_wallet_count"]:
                    _log.info(f"SKIP ${symbol}: {wallet_count} wallets < {config['min_wallet_count']}")
                    continue

            # 4j. Score gate
            if score < config["min_agiotage_score"]:
                continue

            # Source count check
            if len(sources) < config["min_sources"]:
                continue

            # 4k. Bot saturation check
            try:
                recent_buys = (await db.execute(
                    select(func.count(func.distinct(PaperTrade.position_id)))
                    .join(PaperPosition, PaperTrade.position_id == PaperPosition.id)
                    .where(PaperPosition.token_address == signal.token_address,
                           PaperTrade.action == "BUY",
                           PaperTrade.executed_at >= datetime.utcnow() - timedelta(seconds=60))
                )).scalar() or 0
                if recent_buys >= config["bot_saturation_threshold"]:
                    _log.info(f"SKIP ${symbol}: bot saturation ({recent_buys} buys in 60s)")
                    continue
            except Exception:
                pass

            # 4l. Position size by score
            position_sol = _position_sol_for_score(score, config)

            # 4m. Convert SOL to USD
            sol_price = await _get_sol_price()
            actual_size_usd = position_sol * sol_price

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

            # 4n. Execute live buy if enabled
            live_tx = None
            if live_mode:
                # Re-check kill switch right before execution
                try:
                    from ..core.redis import redis_client
                    if (await redis_client.get("trading:paused")) == "1":
                        live_mode = False
                except Exception:
                    live_mode = False

            if live_mode:
                try:
                    from ..services.jupiter_swap import buy_token
                    live_tx = await buy_token(signal.token_address, position_sol,
                                              slippage_bps=config["buy_slippage_bps"])
                    if not live_tx.get("success"):
                        _log.error(f"LIVE BUY FAILED: ${symbol} -- {live_tx.get('error')}")
                        # Don't create position if live buy fails — we don't own the tokens
                        continue
                    else:
                        _log.info(f"LIVE BUY: ${symbol} tx={live_tx.get('tx_hash')}")
                        # Use actual execution data if available
                        quote = live_tx.get("quote", {})
                        if quote.get("out_amount"):
                            _log.info(f"LIVE BUY filled: received {quote['out_amount']} tokens")
                except Exception as e:
                    _log.error(f"LIVE BUY ERROR: ${symbol} -- {e}")
                    # Don't create position on error — we don't own the tokens
                    continue

            # 4o. Record position and trade
            position = PaperPosition(
                token_address=signal.token_address,
                token_symbol=symbol,
                entry_price=Decimal(str(price)),
                entry_mc=Decimal(str(mc)),
                position_size_usd=Decimal(str(round(actual_size_usd, 2))),
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

            reason = f"Signal: {signal.signal_strength}, {wallet_count}w, Score: {score}, {position_sol} SOL"
            if live_tx and live_tx.get("success"):
                reason += f" [LIVE tx:{live_tx['tx_hash'][:12]}]"

            trade = PaperTrade(
                position_id=0,
                action="BUY",
                pct_of_position=100,
                price=Decimal(str(price)),
                usd_value=Decimal(str(round(actual_size_usd, 2))),
                pnl_pct=Decimal("0"),
                reason=reason[:100],
            )

            await db.commit()
            await db.refresh(position)
            trade.position_id = position.id
            db.add(trade)
            await db.commit()

            mode_label = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
            _log.info(f"{mode_label} BUY: ${symbol} @ ${price:.10f} MC=${mc:,.0f} Score={score} "
                       f"Size={position_sol} SOL (${actual_size_usd:.2f}) [{','.join(sources)}]")

            # Telegram alert
            ca = signal.token_address
            tx_line = f"\nTX: `{live_tx['tx_hash'][:20]}...`" if live_tx and live_tx.get("success") else ""
            msg = (
                f"{'$' if mode_label == 'LIVE' else '#'} *{mode_label} BUY: ${symbol}*\n\n"
                f"Price: ${price:.8f}\n"
                f"MC: ${mc:,.0f}\n"
                f"Liquidity: ${liquidity:,.0f}\n"
                f"Size: {position_sol} SOL (${actual_size_usd:.2f})\n"
                f"Score: {score}\n"
                f"Sources: {', '.join(sources)}{tx_line}\n\n"
                f"CA: `{ca}`\n"
                f"[Chart](https://dexscreener.com/solana/{ca})"
            )
            await _send_telegram(msg)


# === POSITION MANAGEMENT ===

# Module-level flag for rapid polling
_rapid_poll_needed = False


async def _manage_positions():
    global _rapid_poll_needed
    config = await get_config()
    _rapid_poll_needed = False

    async with async_session() as db:
        positions = (await db.execute(
            select(PaperPosition).where(PaperPosition.status == "OPEN")
        )).scalars().all()

        for pos in positions:
            price, mc = await _get_price_mc(pos.token_address)
            if price <= 0:
                continue

            entry = float(pos.entry_price)
            if entry <= 0:
                continue
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

            # 1c. Rapid poll check -- if within threshold% of any TP level or SL
            threshold = config.get("rapid_poll_threshold_pct", 5)
            sl_pct = config["stop_loss_pct"]
            for tp in config["take_profit_levels"]:
                if abs(pnl_pct - tp["at_profit_pct"]) <= threshold:
                    _rapid_poll_needed = True
                    break
            if abs(pnl_pct - (-sl_pct)) <= threshold:
                _rapid_poll_needed = True

            # Check if first TP was already hit (for breakeven stop)
            first_tp_hit = False
            if config["breakeven_stop_after_first_tp"]:
                first_tp = config["take_profit_levels"][0] if config["take_profit_levels"] else None
                if first_tp:
                    existing_first_tp = (await db.execute(
                        select(PaperTrade)
                        .where(PaperTrade.position_id == pos.id,
                               PaperTrade.reason.contains(f"TP {first_tp['at_profit_pct']}%"))
                    )).scalar_one_or_none()
                    if existing_first_tp:
                        first_tp_hit = True

            # 1d. Check take profit levels (5-tier)
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
                    live_tx = await _live_sell(pos.token_address, sell_pct,
                                               f"TP {tp['at_profit_pct']}% ${pos.token_symbol}",
                                               slippage_bps=config["sell_slippage_bps"])
                    tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]" if live_tx and live_tx.get("success") else ""

                    usd_val = float(pos.position_size_usd) * (sell_pct / 100) * (1 + pnl_pct / 100)
                    trade = PaperTrade(
                        position_id=pos.id, action="SELL",
                        pct_of_position=sell_pct, price=Decimal(str(price)),
                        usd_value=Decimal(str(round(usd_val, 2))),
                        pnl_pct=Decimal(str(round(pnl_pct, 4))),
                        reason=f"TP {tp['at_profit_pct']}% hit -- sold {sell_pct}%{tx_tag}"[:100],
                    )
                    db.add(trade)
                    pos.remaining_pct = Decimal(str(remaining - sell_pct))
                    remaining -= sell_pct

                    mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                    _log.info(f"{mode} SELL (TP): ${pos.token_symbol} {sell_pct}% @ +{pnl_pct:.1f}%")

                    await _send_telegram(
                        f"*{mode} SELL: ${pos.token_symbol}*\n"
                        f"TP {tp['at_profit_pct']}% hit\n"
                        f"Sold {sell_pct}% @ +{pnl_pct:.1f}%\n"
                        f"Value: ${usd_val:.2f}"
                    )

                    # 1e. After first TP hit, move stop to breakeven + 2%
                    if not first_tp_hit:
                        first_tp_hit = True

            # 1f. Check stop loss (with breakeven stop adjustment)
            if first_tp_hit and config["breakeven_stop_after_first_tp"]:
                # After first TP hit, stop loss moves to entry + 2% (protect profits)
                # Triggers if PnL drops below +2% (i.e., nearly back to breakeven)
                sl_triggered = pnl_pct <= 2.0
            else:
                sl_triggered = pnl_pct <= -config["stop_loss_pct"]

            if sl_triggered and remaining > 0:
                slippage = config["sell_slippage_bps"]
                close_reason = f"Stop loss ({pnl_pct:.1f}%)"
                if first_tp_hit and config["breakeven_stop_after_first_tp"]:
                    close_reason = f"Breakeven stop ({pnl_pct:.1f}%)"

                live_tx = await _live_sell(pos.token_address, 100,
                                           f"SL ${pos.token_symbol}", slippage_bps=slippage)
                tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]" if live_tx and live_tx.get("success") else ""

                usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                trade = PaperTrade(
                    position_id=pos.id, action="SELL",
                    pct_of_position=remaining, price=Decimal(str(price)),
                    usd_value=Decimal(str(round(usd_val, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=f"{close_reason}{tx_tag}"[:100],
                )
                db.add(trade)
                pos.remaining_pct = Decimal("0")
                pos.status = "CLOSED"
                pos.close_reason = close_reason
                pos.closed_at = datetime.utcnow()

                # Track daily loss if it was a loss
                if pnl_pct < 0:
                    sol_price = await _get_sol_price()
                    if sol_price > 0:
                        loss_sol = abs(float(pos.position_size_usd) * (remaining / 100) * (pnl_pct / 100)) / sol_price
                        await _track_daily_loss(loss_sol)

                mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                _log.info(f"{mode} SELL (SL): ${pos.token_symbol} 100% @ {pnl_pct:.1f}%")
                await _send_telegram(
                    f"*{mode} STOP LOSS: ${pos.token_symbol}*\n"
                    f"{close_reason}\n"
                    f"Sold {remaining:.0f}% @ {pnl_pct:.1f}%\n"
                    f"Value: ${usd_val:.2f}"
                )
                continue  # Position closed, skip further checks

            # 1g. Check trailing stop
            if config["trailing_stop_enabled"] and remaining > 0:
                highest_pnl = ((highest - entry) / entry) * 100
                if highest_pnl >= config["trailing_stop_activation_pct"]:
                    trail_from = highest * (1 - config["trailing_stop_trail_pct"] / 100)
                    if price <= trail_from:
                        live_tx = await _live_sell(pos.token_address, 100,
                                                   f"TRAIL ${pos.token_symbol}",
                                                   slippage_bps=config["sell_slippage_bps"])
                        tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]" if live_tx and live_tx.get("success") else ""

                        usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                        trade = PaperTrade(
                            position_id=pos.id, action="SELL",
                            pct_of_position=remaining, price=Decimal(str(price)),
                            usd_value=Decimal(str(round(usd_val, 2))),
                            pnl_pct=Decimal(str(round(pnl_pct, 4))),
                            reason=f"Trailing stop (peak +{highest_pnl:.1f}%, now +{pnl_pct:.1f}%){tx_tag}"[:100],
                        )
                        db.add(trade)
                        pos.remaining_pct = Decimal("0")
                        pos.status = "CLOSED"
                        pos.close_reason = f"Trailing stop (+{pnl_pct:.1f}% from +{highest_pnl:.1f}% peak)"
                        pos.closed_at = datetime.utcnow()

                        # Track daily loss if applicable
                        if pnl_pct < 0:
                            sol_price = await _get_sol_price()
                            if sol_price > 0:
                                loss_sol = abs(float(pos.position_size_usd) * (remaining / 100) * (pnl_pct / 100)) / sol_price
                                await _track_daily_loss(loss_sol)

                        mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                        _log.info(f"{mode} SELL (TRAIL): ${pos.token_symbol} @ +{pnl_pct:.1f}% (peak +{highest_pnl:.1f}%)")
                        await _send_telegram(
                            f"*{mode} TRAILING STOP: ${pos.token_symbol}*\n"
                            f"Peak: +{highest_pnl:.1f}%\n"
                            f"Sold @ +{pnl_pct:.1f}%\n"
                            f"Value: ${usd_val:.2f}"
                        )
                        continue  # Position closed

            # 1h. Check max holding time
            age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
            if age_hours >= config["max_holding_hours"] and remaining > 0:
                live_tx = await _live_sell(pos.token_address, 100,
                                           f"MAX HOLD ${pos.token_symbol}",
                                           slippage_bps=config["sell_slippage_bps"])
                tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]" if live_tx and live_tx.get("success") else ""

                usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                trade = PaperTrade(
                    position_id=pos.id, action="SELL",
                    pct_of_position=remaining, price=Decimal(str(price)),
                    usd_value=Decimal(str(round(usd_val, 2))),
                    pnl_pct=Decimal(str(round(pnl_pct, 4))),
                    reason=f"Max hold time ({config['max_holding_hours']}h) @ {pnl_pct:.1f}%{tx_tag}"[:100],
                )
                db.add(trade)
                pos.remaining_pct = Decimal("0")
                pos.status = "CLOSED"
                pos.close_reason = f"Max hold time ({pnl_pct:.1f}%)"
                pos.closed_at = datetime.utcnow()

                # Track daily loss if applicable
                if pnl_pct < 0:
                    sol_price = await _get_sol_price()
                    if sol_price > 0:
                        loss_sol = abs(float(pos.position_size_usd) * (remaining / 100) * (pnl_pct / 100)) / sol_price
                        await _track_daily_loss(loss_sol)

                mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                _log.info(f"{mode} SELL (MAX HOLD): ${pos.token_symbol} @ {pnl_pct:.1f}% after {age_hours:.1f}h")
                await _send_telegram(
                    f"*{mode} MAX HOLD: ${pos.token_symbol}*\n"
                    f"Held {age_hours:.1f}h (max {config['max_holding_hours']}h)\n"
                    f"Sold @ {pnl_pct:.1f}%\n"
                    f"Value: ${usd_val:.2f}"
                )
                continue  # Position closed

            # 1i. Deployer dump detection -- check if deployer sold >5% of supply
            # Only check once per position per 60 seconds to prevent duplicate panic sells
            _dump_check_key = f"dump_checked:{pos.id}"
            _dump_already_checked = False
            try:
                from ..core.redis import redis_client
                if await redis_client.get(_dump_check_key):
                    _dump_already_checked = True
                else:
                    await redis_client.set(_dump_check_key, "1", ex=60)
            except Exception:
                pass
            if not _dump_already_checked and (signal_has_deployer := pos.entry_sources and "deployer" in (pos.entry_sources or "")):
                try:
                    from ..models.platform import TopDeployer
                    from .smart_money_tracker import TokenDeployment
                    td = (await db.execute(
                        select(TokenDeployment).where(TokenDeployment.token_address == pos.token_address)
                    )).scalar_one_or_none()
                    if td and td.deployer_wallet:
                        # Check GMGN for deployer sells
                        api_key = os.getenv("GMGN_API_KEY", "")
                        if api_key:
                            try:
                                async with httpx.AsyncClient() as client:
                                    resp = await client.get(
                                        f"https://openapi.gmgn.ai/v1/token/security",
                                        params={"chain": "sol", "address": pos.token_address,
                                                "timestamp": int(time.time()), "client_id": str(uuid.uuid4())},
                                        headers={"X-APIKEY": api_key}, timeout=8)
                                    if resp.status_code == 200:
                                        sec_data = resp.json().get("data", {})
                                        creator_pct = float(sec_data.get("creator_token_status", {}).get("sell_pct", 0) or 0)
                                        if creator_pct > 5:
                                            # PANIC SELL -- deployer dumped
                                            live_tx = await _live_sell(
                                                pos.token_address, 100,
                                                f"DEPLOYER DUMP ${pos.token_symbol}",
                                                slippage_bps=config["panic_slippage_bps"])
                                            tx_tag = f" [LIVE tx:{live_tx['tx_hash'][:12]}]" if live_tx and live_tx.get("success") else ""

                                            usd_val = float(pos.position_size_usd) * (remaining / 100) * (1 + pnl_pct / 100)
                                            trade = PaperTrade(
                                                position_id=pos.id, action="SELL",
                                                pct_of_position=remaining, price=Decimal(str(price)),
                                                usd_value=Decimal(str(round(usd_val, 2))),
                                                pnl_pct=Decimal(str(round(pnl_pct, 4))),
                                                reason=f"DEPLOYER DUMP ({creator_pct:.1f}% sold){tx_tag}"[:100],
                                            )
                                            db.add(trade)
                                            pos.remaining_pct = Decimal("0")
                                            pos.status = "CLOSED"
                                            pos.close_reason = f"Deployer dump ({creator_pct:.1f}% sold)"
                                            pos.closed_at = datetime.utcnow()

                                            if pnl_pct < 0:
                                                sol_price = await _get_sol_price()
                                                if sol_price > 0:
                                                    loss_sol = abs(float(pos.position_size_usd) * (remaining / 100) * (pnl_pct / 100)) / sol_price
                                                    await _track_daily_loss(loss_sol)

                                            mode = "LIVE" if live_tx and live_tx.get("success") else "PAPER"
                                            _log.warning(f"{mode} PANIC SELL (DEPLOYER DUMP): ${pos.token_symbol} -- "
                                                          f"deployer sold {creator_pct:.1f}% @ {pnl_pct:.1f}%")
                                            await _send_telegram(
                                                f"*{mode} DEPLOYER DUMP: ${pos.token_symbol}*\n"
                                                f"Deployer sold {creator_pct:.1f}% of supply!\n"
                                                f"PANIC SOLD @ {pnl_pct:.1f}%\n"
                                                f"Value: ${usd_val:.2f}"
                                            )
                                            # Mark as closed and skip rest
                                            remaining = 0
                            except Exception:
                                pass
                except Exception:
                    pass

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
    _log.info("Agiotage Meme Trading Bot v2 starting")
    await asyncio.sleep(55)

    config = await get_config()
    _log.info(f"Config: score>={config['min_agiotage_score']}, MC={config['min_mc']:,}-{config['max_mc']:,}, "
              f"base={config['base_position_sol']} SOL, max={config['max_open_positions']} positions, "
              f"SL={config['stop_loss_pct']}%, trail={config['trailing_stop_trail_pct']}%, "
              f"daily_limit={config['daily_loss_limit_sol']} SOL, "
              f"TP tiers={len(config['take_profit_levels'])}")

    while True:
        try:
            await _check_for_entries()
            await _manage_positions()
        except Exception as e:
            _log.error(f"Paper trader error: {e}")

        # Use rapid poll interval if any position is near a TP/SL level
        interval = RAPID_POLL if _rapid_poll_needed else NORMAL_POLL
        await asyncio.sleep(interval)

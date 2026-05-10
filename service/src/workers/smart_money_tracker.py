# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Smart Money Wallet Tracker — monitors proven profitable wallets and detects cluster signals."""
import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from collections import defaultdict

import httpx
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session

_log = logging.getLogger("smart-money")

GMGN_HOST = "https://openapi.gmgn.ai"
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")

POLL_INTERVAL = 15
LEADERBOARD_REFRESH = 14400  # 4 hours
CLUSTER_WINDOW_MINUTES = 30
CLUSTER_MIN_WALLETS = 3


async def _gmgn_get(path: str, params: dict, client: httpx.AsyncClient) -> dict | None:
    if not GMGN_API_KEY:
        return None
    query = {**params, "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
    try:
        resp = await client.get(f"{GMGN_HOST}{path}", params=query,
                                headers={"X-APIKEY": GMGN_API_KEY}, timeout=15)
        if resp.status_code == 429:
            _log.warning("GMGN rate limited")
            await asyncio.sleep(30)
            return None
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        _log.debug(f"GMGN request failed: {e}")
        return None


# ==================== DB MODELS (inline to avoid circular imports) ====================
# These tables are created in the lifespan startup via create_all

from sqlalchemy import (
    String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Index
)
from sqlalchemy.orm import Mapped, mapped_column
from ..models.base import Base


class SmartMoneyWallet(Base):
    __tablename__ = "smart_money_wallets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String(66), nullable=False, unique=True)
    chain: Mapped[str] = mapped_column(String(20), default="solana")
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    twitter: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    winrate: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    realized_profit: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    tokens_traded: Mapped[int] = mapped_column(Integer, default=0)
    avg_holding_period: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    pnl_2x_plus: Mapped[int] = mapped_column(Integer, default=0)
    pnl_5x_plus: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    tier: Mapped[str] = mapped_column(String(10), default="B")
    funded_from: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_active: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_stats_update: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        Index("idx_sm_score", "score"),
        Index("idx_sm_tier", "tier"),
    )


class SmartMoneyTrade(Base):
    __tablename__ = "smart_money_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    wallet: Mapped[str] = mapped_column(String(66), nullable=False)
    wallet_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wallet_twitter: Mapped[str | None] = mapped_column(String(100), nullable=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    amount_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    price_usd: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    is_full_position: Mapped[bool] = mapped_column(Boolean, default=False)
    is_kol: Mapped[bool] = mapped_column(Boolean, default=False)
    trade_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_smt_token_time", "token_address", "trade_time"),
        Index("idx_smt_wallet", "wallet"),
    )


class ClusterSignal(Base):
    __tablename__ = "cluster_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    wallet_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    full_position_count: Mapped[int] = mapped_column(Integer, default=0)
    kol_count: Mapped[int] = mapped_column(Integer, default=0)
    signal_strength: Mapped[str] = mapped_column(String(20), default="MEDIUM")
    weighted_score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    avg_wallet_winrate: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    is_deployer_token: Mapped[bool] = mapped_column(Boolean, default=False)
    wallets_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Accuracy tracking — filled in by accuracy checker
    price_at_signal: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    mc_at_signal: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    highest_mc: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    price_1h: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    price_6h: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    price_24h: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pct_change_1h: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    pct_change_6h: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    pct_change_24h: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    __table_args__ = (
        Index("idx_cluster_time", "detected_at"),
        Index("idx_cluster_token", "token_address"),
    )


# ==================== CORE LOGIC ====================

def _calc_wallet_score(winrate: float, realized_profit: float, trades: int,
                       pnl_2x: int, pnl_5x: int, tokens: int) -> float:
    """Score a wallet. Higher = more consistently profitable.
    Factors: win rate, profit, consistency (2x+ ratio), sample size.
    """
    if trades < 5 or tokens < 3:
        return 0

    wr_score = winrate * 100
    profit_score = min(realized_profit / 100, 50)
    consistency = ((pnl_2x + pnl_5x * 2) / max(tokens, 1)) * 30
    size_bonus = min(tokens / 10, 10)

    return round(wr_score + profit_score + consistency + size_bonus, 2)


def _calc_wallet_tier(score: float, winrate: float, pnl_5x: int) -> str:
    """Tier a wallet based on score.
    S = elite (score 150+, 70%+ WR)
    A = strong (score 100+, 60%+ WR)
    B = solid (score 60+)
    C = average
    """
    if score >= 150 and winrate >= 0.70 and pnl_5x >= 2:
        return "S"
    if score >= 100 and winrate >= 0.60:
        return "A"
    if score >= 60:
        return "B"
    return "C"


def _calc_signal_strength(wallet_count: int, full_positions: int, kol_count: int,
                          total_usd: float, avg_winrate: float = 0,
                          is_deployer_token: bool = False, weighted_score: float = 0) -> str:
    """Rate the strength of a cluster signal. Weighted by wallet quality."""
    # Base strength from volume
    if wallet_count >= 5 and full_positions >= 3 and kol_count >= 1:
        base = "VERY_STRONG"
    elif wallet_count >= 4 or (wallet_count >= 3 and kol_count >= 1):
        base = "STRONG"
    elif wallet_count >= 3 or (wallet_count >= 2 and full_positions >= 2):
        base = "MEDIUM"
    else:
        base = "WEAK"

    # Upgrade if wallets have high win rates
    if avg_winrate >= 0.65 and base in ("MEDIUM", "STRONG"):
        upgrade = {"MEDIUM": "STRONG", "STRONG": "VERY_STRONG"}
        base = upgrade.get(base, base)

    # Upgrade if token is from a top deployer (convergence signal)
    if is_deployer_token and base in ("MEDIUM", "STRONG"):
        upgrade = {"MEDIUM": "STRONG", "STRONG": "VERY_STRONG"}
        base = upgrade.get(base, base)

    # Downgrade if wallets are low quality
    if avg_winrate > 0 and avg_winrate < 0.35 and base in ("STRONG", "VERY_STRONG"):
        downgrade = {"VERY_STRONG": "STRONG", "STRONG": "MEDIUM"}
        base = downgrade.get(base, base)

    return base


async def _poll_smart_money_trades():
    """Fetch recent smart money and KOL trades, store new ones."""
    async with httpx.AsyncClient() as client:
        sm_data = await _gmgn_get("/v1/user/smartmoney", {"chain": "sol", "limit": 200}, client)
        kol_data = await _gmgn_get("/v1/user/kol", {"chain": "sol", "limit": 100}, client)

    all_trades = []

    for source, data, is_kol in [("smartmoney", sm_data, False), ("kol", kol_data, True)]:
        if not data:
            continue
        items = data if isinstance(data, list) else data.get("data", data)
        if isinstance(items, dict):
            items = items.get("list", items.get("trades", []))
        if not isinstance(items, list):
            continue
        for t in items:
            all_trades.append({**t, "_is_kol": is_kol})

    if not all_trades:
        return

    async with async_session() as db:
        existing_hashes = set((await db.execute(
            select(SmartMoneyTrade.tx_hash)
            .where(SmartMoneyTrade.created_at >= datetime.utcnow() - timedelta(hours=2))
        )).scalars().all())

        new_count = 0
        for t in all_trades:
            tx_hash = t.get("transaction_hash", "")
            if not tx_hash or tx_hash in existing_hashes:
                continue

            trade_time = datetime.utcfromtimestamp(t.get("timestamp", 0)) if t.get("timestamp") else datetime.utcnow()

            maker_info = t.get("maker_info", {})
            w_name = maker_info.get("name") or maker_info.get("twitter_name") or ""
            w_twitter = maker_info.get("twitter_username") or ""

            trade = SmartMoneyTrade(
                tx_hash=tx_hash,
                wallet=t.get("maker", ""),
                wallet_name=w_name[:100],
                wallet_twitter=w_twitter[:100],
                token_address=t.get("base_address", ""),
                token_symbol=t.get("base_token", {}).get("symbol", "")[:20],
                side=t.get("side", ""),
                amount_usd=Decimal(str(t.get("amount_usd", 0) or 0)),
                price_usd=Decimal(str(t.get("price_usd", 0) or 0)),
                is_full_position=(t.get("is_open_or_close") == 0),
                is_kol=t.get("_is_kol", False),
                trade_time=trade_time,
            )
            db.add(trade)
            existing_hashes.add(tx_hash)
            new_count += 1

            # Track wallet discovery
            wallet_addr = t.get("maker", "")
            if wallet_addr:
                existing_wallet = (await db.execute(
                    select(SmartMoneyWallet).where(SmartMoneyWallet.wallet == wallet_addr)
                )).scalar_one_or_none()
                if not existing_wallet:
                    maker_info = t.get("maker_info", {})
                    tags = ",".join(maker_info.get("tags", []))
                    wallet = SmartMoneyWallet(
                        wallet=wallet_addr,
                        name=maker_info.get("name") or maker_info.get("twitter_name") or "",
                        twitter=maker_info.get("twitter_username") or "",
                        tags=tags,
                        last_active=trade_time,
                    )
                    db.add(wallet)

        if new_count:
            await db.commit()
            _log.info(f"Stored {new_count} new smart money trades")

        # Run cluster detection
        await _detect_clusters(db)


async def _detect_clusters(db: AsyncSession):
    """Detect clusters with wallet weighting, wash trade filtering, and deployer convergence."""
    cutoff = datetime.utcnow() - timedelta(minutes=CLUSTER_WINDOW_MINUTES)

    recent_buys = (await db.execute(
        select(SmartMoneyTrade)
        .where(SmartMoneyTrade.side == "buy",
               SmartMoneyTrade.trade_time >= cutoff,
               SmartMoneyTrade.amount_usd >= 25)  # Filter micro trades (likely rug token sniping)
        .order_by(SmartMoneyTrade.trade_time.desc())
    )).scalars().all()

    # IMPROVEMENT #3: Filter wash trading — exclude wallets that bought AND sold same token in window
    recent_sells = set()
    sell_trades = (await db.execute(
        select(SmartMoneyTrade.wallet, SmartMoneyTrade.token_address)
        .where(SmartMoneyTrade.side == "sell", SmartMoneyTrade.trade_time >= cutoff)
    )).all()
    for wallet, token in sell_trades:
        recent_sells.add((wallet, token))

    filtered_buys = [t for t in recent_buys if (t.wallet, t.token_address) not in recent_sells]

    # Filter out wrapped tokens and stablecoins that aren't real signals
    SKIP_SYMBOLS = {"WSOL", "WETH", "WBTC", "USDC", "USDT", "DAI", "SOL"}
    filtered_buys = [t for t in filtered_buys if (t.token_symbol or "").upper() not in SKIP_SYMBOLS]

    # Group by token
    clusters = defaultdict(list)
    for trade in filtered_buys:
        clusters[trade.token_address].append(trade)

    import json as _json
    for token_addr, trades in clusters.items():
        unique_wallets = set(t.wallet for t in trades)
        if len(unique_wallets) < CLUSTER_MIN_WALLETS:
            continue

        existing_signal = (await db.execute(
            select(ClusterSignal)
            .where(ClusterSignal.token_address == token_addr,
                   ClusterSignal.detected_at >= cutoff)
        )).scalar_one_or_none()
        if existing_signal:
            continue

        total_usd = sum(float(t.amount_usd or 0) for t in trades)
        if total_usd < 100:
            continue

        # Check MC — skip micro cap tokens under $100K (pump & dump noise)
        current_mc = 0
        try:
            async with httpx.AsyncClient() as mc_client:
                mc_resp = await mc_client.get(
                    f"https://api.dexscreener.com/token-pairs/v1/solana/{token_addr}", timeout=8)
                if mc_resp.status_code == 200:
                    mc_data = mc_resp.json()
                    pairs = mc_data if isinstance(mc_data, list) else mc_data.get("pairs", [])
                    if pairs:
                        current_mc = float(pairs[0].get("fdv", 0) or 0)
        except Exception:
            pass

        symbol = trades[0].token_symbol or ""

        # Block if MC is under $100K OR if we couldn't determine MC (likely garbage)
        if current_mc < 100_000:
            _log.debug(f"Skipping {symbol} — MC ${current_mc:,.0f} under $100K threshold")
            continue
        full_positions = sum(1 for t in trades if t.is_full_position)
        kol_count = len(set(t.wallet for t in trades if t.is_kol))

        # IMPROVEMENT #2: Look up wallet scores and calculate weighted quality
        wallet_data = {}
        winrates = []
        scores = []
        for addr in unique_wallets:
            w = (await db.execute(select(SmartMoneyWallet).where(SmartMoneyWallet.wallet == addr))).scalar_one_or_none()
            if w:
                wallet_data[addr] = {"name": w.name or "", "twitter": w.twitter or "",
                                     "tier": w.tier or "", "score": float(w.score or 0),
                                     "winrate": float(w.winrate or 0)}
                if w.winrate and float(w.winrate) > 0:
                    winrates.append(float(w.winrate))
                if w.score and float(w.score) > 0:
                    scores.append(float(w.score))
            else:
                wallet_data[addr] = {"name": "", "twitter": "", "tier": "", "score": 0, "winrate": 0}

        avg_winrate = sum(winrates) / len(winrates) if winrates else 0
        avg_score = sum(scores) / len(scores) if scores else 0

        # IMPROVEMENT #4: Check if this token is from a top deployer
        from ..models.platform import MemeDeployment, TopDeployer
        token_deployment = (await db.execute(
            select(MemeDeployment).where(MemeDeployment.mint_address == token_addr)
        )).scalar_one_or_none()

        is_deployer_token = False
        if token_deployment and token_deployment.deployer_wallet:
            top_deployer = (await db.execute(
                select(TopDeployer).where(TopDeployer.wallet == token_deployment.deployer_wallet)
            )).scalar_one_or_none()
            is_deployer_token = top_deployer is not None

        # Calculate strength with all improvements
        strength = _calc_signal_strength(
            len(unique_wallets), full_positions, kol_count, total_usd,
            avg_winrate=avg_winrate, is_deployer_token=is_deployer_token,
            weighted_score=avg_score
        )

        # Get current token price for accuracy tracking (#1)
        current_price = float(trades[0].price_usd or 0) if trades else 0

        # Build deduplicated wallet info
        wallet_agg = {}
        for t in trades:
            if t.wallet not in wallet_agg:
                wd = wallet_data.get(t.wallet, {})
                wallet_agg[t.wallet] = {
                    "wallet": t.wallet[:12] + "...", "wallet_full": t.wallet,
                    "name": wd.get("name", ""), "twitter": wd.get("twitter", ""),
                    "tier": wd.get("tier", ""), "score": wd.get("score", 0),
                    "winrate": wd.get("winrate", 0),
                    "usd": float(t.amount_usd or 0),
                    "full_open": t.is_full_position, "kol": t.is_kol, "count": 1,
                }
            else:
                wallet_agg[t.wallet]["usd"] += float(t.amount_usd or 0)
                wallet_agg[t.wallet]["count"] += 1
        wallets_info = list(wallet_agg.values())

        signal = ClusterSignal(
            token_address=token_addr, token_symbol=symbol,
            wallet_count=len(unique_wallets),
            total_usd=Decimal(str(total_usd)),
            full_position_count=full_positions, kol_count=kol_count,
            signal_strength=strength,
            weighted_score=Decimal(str(round(avg_score, 2))),
            avg_wallet_winrate=Decimal(str(round(avg_winrate, 4))),
            is_deployer_token=is_deployer_token,
            wallets_json=_json.dumps(wallets_info),
            price_at_signal=Decimal(str(current_price)) if current_price else None,
            mc_at_signal=Decimal(str(current_mc)) if current_mc else None,
            highest_mc=Decimal(str(current_mc)) if current_mc else None,
        )
        db.add(signal)

        convergence = " + TOP DEPLOYER" if is_deployer_token else ""
        _log.warning(
            f"CLUSTER SIGNAL [{strength}]: {symbol} ({token_addr[:12]}...) — "
            f"{len(unique_wallets)} wallets (avg WR {avg_winrate:.0%}), ${total_usd:,.0f}, "
            f"{full_positions} full opens, {kol_count} KOLs{convergence}"
        )

        from ..models.platform import Notification
        body = f"{len(unique_wallets)} smart money wallets buying {symbol}. ${total_usd:,.0f} total."
        if avg_winrate > 0:
            body += f" Avg win rate: {avg_winrate:.0%}."
        if is_deployer_token:
            body += " TOKEN FROM TOP DEPLOYER."
        notif = Notification(
            agent_id="0xb18a31796ea51c52c203c96aab0b1bc551c4e051",
            type="smart_money_alert",
            title=f"Cluster Signal [{strength}]: ${symbol}",
            body=body,
            link="/trading.html",
        )
        db.add(notif)

    await db.commit()


async def _refresh_wallet_leaderboard():
    """Update wallet stats and scoring for the leaderboard."""
    async with async_session() as db:
        wallets = (await db.execute(
            select(SmartMoneyWallet)
            .where(SmartMoneyWallet.last_stats_update.is_(None) |
                   (SmartMoneyWallet.last_stats_update < datetime.utcnow() - timedelta(hours=4)))
            .order_by(SmartMoneyWallet.score.desc().nullslast())
            .limit(30)
        )).scalars().all()

        if not wallets:
            return

        _log.info(f"Refreshing stats for {len(wallets)} smart money wallets")

        async with httpx.AsyncClient() as client:
            for w in wallets:
                data = await _gmgn_get(
                    "/v1/user/wallet_stats",
                    {"chain": "sol", "wallet_address": w.wallet, "period": "7d"},
                    client,
                )
                if not data:
                    await asyncio.sleep(1.5)
                    continue

                stats = data.get("data", data)
                if isinstance(stats, list) and stats:
                    stats = stats[0]
                if not isinstance(stats, dict):
                    await asyncio.sleep(1.5)
                    continue

                pnl = stats.get("pnl_stat", {})
                common = stats.get("common", {})

                winrate = float(pnl.get("winrate", 0) or 0)
                realized = float(stats.get("realized_profit", 0) or 0)
                buy_count = int(stats.get("buy", 0) or 0)
                sell_count = int(stats.get("sell", 0) or 0)
                tokens = int(pnl.get("token_num", 0) or 0)
                pnl_2x = int(pnl.get("pnl_2x_5x_num", 0) or 0)
                pnl_5x = int(pnl.get("pnl_gt_5x_num", 0) or 0)
                avg_hold = float(pnl.get("avg_holding_period", 0) or 0)

                w.winrate = Decimal(str(round(winrate, 4)))
                w.realized_profit = Decimal(str(realized))
                w.total_trades = buy_count + sell_count
                w.tokens_traded = tokens
                w.pnl_2x_plus = pnl_2x
                w.pnl_5x_plus = pnl_5x
                w.avg_holding_period = Decimal(str(avg_hold))
                w.funded_from = common.get("fund_from", "")
                w.name = common.get("name") or common.get("twitter_name") or w.name
                w.twitter = common.get("twitter_username") or w.twitter
                w.tags = ",".join(common.get("tags", []))

                score = _calc_wallet_score(winrate, realized, buy_count + sell_count, pnl_2x, pnl_5x, tokens)
                tier = _calc_wallet_tier(score, winrate, pnl_5x)

                w.score = Decimal(str(score))
                w.tier = tier
                w.last_stats_update = datetime.utcnow()

                _log.info(f"Wallet {w.wallet[:12]}... WR={winrate:.0%} profit=${realized:,.0f} score={score} tier={tier}")

                await asyncio.sleep(1.5)

        await db.commit()
        _log.info("Wallet leaderboard refresh complete")


async def _check_signal_accuracy():
    """Check how signals performed — track price at 1h, 6h, 24h after signal."""
    async with async_session() as db:
        # Find signals that need price checks
        now = datetime.utcnow()

        # Signals 1+ hours old without 1h price
        signals_1h = (await db.execute(
            select(ClusterSignal)
            .where(ClusterSignal.price_at_signal.isnot(None),
                   ClusterSignal.price_1h.is_(None),
                   ClusterSignal.detected_at <= now - timedelta(hours=1),
                   ClusterSignal.detected_at >= now - timedelta(hours=2))
            .limit(20)
        )).scalars().all()

        # Signals 6+ hours old without 6h price
        signals_6h = (await db.execute(
            select(ClusterSignal)
            .where(ClusterSignal.price_at_signal.isnot(None),
                   ClusterSignal.price_6h.is_(None),
                   ClusterSignal.detected_at <= now - timedelta(hours=6),
                   ClusterSignal.detected_at >= now - timedelta(hours=7))
            .limit(20)
        )).scalars().all()

        # Signals 24+ hours old without 24h price
        signals_24h = (await db.execute(
            select(ClusterSignal)
            .where(ClusterSignal.price_at_signal.isnot(None),
                   ClusterSignal.price_24h.is_(None),
                   ClusterSignal.detected_at <= now - timedelta(hours=24),
                   ClusterSignal.detected_at >= now - timedelta(hours=25))
            .limit(20)
        )).scalars().all()

        all_signals = [(s, "1h") for s in signals_1h] + [(s, "6h") for s in signals_6h] + [(s, "24h") for s in signals_24h]

        if not all_signals:
            return

        _log.info(f"Checking accuracy for {len(all_signals)} signals")

        async with httpx.AsyncClient() as client:
            for signal, period in all_signals:
                # Get current price from GMGN or DexScreener
                try:
                    resp = await client.get(
                        f"https://api.dexscreener.com/token-pairs/v1/solana/{signal.token_address}",
                        timeout=10
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        pairs = data if isinstance(data, list) else data.get("pairs", [])
                        if pairs:
                            current_price = float(pairs[0].get("priceUsd", 0) or 0)
                            fetched_mc = float(pairs[0].get("fdv", 0) or 0)
                            if fetched_mc > 0 and (not signal.highest_mc or fetched_mc > float(signal.highest_mc)):
                                signal.highest_mc = Decimal(str(fetched_mc))
                            signal_price = float(signal.price_at_signal or 0)

                            if signal_price > 0 and current_price > 0:
                                pct_change = (current_price - signal_price) / signal_price

                                if period == "1h":
                                    signal.price_1h = Decimal(str(current_price))
                                    signal.pct_change_1h = Decimal(str(round(pct_change, 4)))
                                elif period == "6h":
                                    signal.price_6h = Decimal(str(current_price))
                                    signal.pct_change_6h = Decimal(str(round(pct_change, 4)))
                                elif period == "24h":
                                    signal.price_24h = Decimal(str(current_price))
                                    signal.pct_change_24h = Decimal(str(round(pct_change, 4)))
                                    # Set final outcome
                                    if pct_change >= 0.5:
                                        signal.outcome = "BIG_WIN"
                                    elif pct_change >= 0.1:
                                        signal.outcome = "WIN"
                                    elif pct_change >= -0.1:
                                        signal.outcome = "NEUTRAL"
                                    elif pct_change >= -0.5:
                                        signal.outcome = "LOSS"
                                    else:
                                        signal.outcome = "BIG_LOSS"

                                    _log.info(f"Signal accuracy: {signal.token_symbol} [{signal.signal_strength}] "
                                              f"24h: {pct_change:+.1%} -> {signal.outcome}")
                except Exception as e:
                    _log.debug(f"Price check failed for {signal.token_address[:12]}: {e}")

                await asyncio.sleep(0.5)

        await db.commit()


async def run():
    """Main loop — poll trades, detect clusters, refresh leaderboard, check accuracy."""
    _log.info("Smart Money Tracker starting — monitoring profitable wallets with accuracy tracking")
    await asyncio.sleep(20)
    cycle = 0
    last_leaderboard = 0

    while True:
        try:
            await _poll_smart_money_trades()

            # Refresh leaderboard every 4 hours
            if time.time() - last_leaderboard > LEADERBOARD_REFRESH:
                await _refresh_wallet_leaderboard()
                last_leaderboard = time.time()

            # Check signal accuracy every 10 cycles (~2.5 min)
            if cycle % 10 == 0:
                await _check_signal_accuracy()

            cycle += 1
        except Exception as e:
            _log.error(f"Smart money tracker error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run())

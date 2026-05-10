# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Wallet Follow — track specific wallets (Discord callers, KOLs, insiders) in real-time.
Detects when tracked wallets buy, feeds into signal convergence.
Self-improving: ranks wallets by actual PNL over time, drops losers, promotes winners.
"""
import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from collections import defaultdict

import httpx
from sqlalchemy import select, func, update, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("wallet-follow")

GMGN_HOST = "https://openapi.gmgn.ai"
GMGN_API_KEY = os.getenv("GMGN_API_KEY", "")
POLL_INTERVAL = 30
STATS_REFRESH_INTERVAL = 7200  # 2 hours
MIN_SCORE_TO_KEEP = -20  # Drop wallets that consistently lose


async def _gmgn_get(path: str, params: dict, client: httpx.AsyncClient) -> dict | None:
    if not GMGN_API_KEY:
        return None
    query = {**params, "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
    try:
        resp = await client.get(f"{GMGN_HOST}{path}", params=query,
                                headers={"X-APIKEY": GMGN_API_KEY}, timeout=15)
        if resp.status_code == 429:
            await asyncio.sleep(30)
            return None
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


# === DB MODELS ===

class FollowedWallet(Base):
    __tablename__ = "followed_wallets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String(66), nullable=False, unique=True)
    chain: Mapped[str] = mapped_column(String(20), default="solana")
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    twitter: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Performance tracking
    winrate: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    realized_profit: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    tokens_traded: Mapped[int] = mapped_column(Integer, default=0)
    pnl_2x_plus: Mapped[int] = mapped_column(Integer, default=0)
    pnl_5x_plus: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    tier: Mapped[str] = mapped_column(String(10), default="NEW")
    # Status
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_discovered: Mapped[bool] = mapped_column(Boolean, default=False)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_stats_update: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_fw_score", "score"),
        Index("idx_fw_active", "active"),
    )


class WalletTrade(Base):
    __tablename__ = "followed_wallet_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String(66), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    amount_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    price_usd: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    trade_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Performance tracking
    price_now: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_fwt_wallet", "wallet"),
        Index("idx_fwt_time", "trade_time"),
        Index("idx_fwt_token", "token_address"),
    )


class WalletSignal(Base):
    __tablename__ = "wallet_follow_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    wallet_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    avg_wallet_score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    strength: Mapped[str] = mapped_column(String(20), default="MEDIUM")
    wallets_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_wfs_time", "detected_at"),
        Index("idx_wfs_token", "token_address"),
    )


# === SCORING ===

def _calc_wallet_score(winrate: float, profit: float, trades: int,
                       pnl_2x: int, pnl_5x: int, tokens: int) -> float:
    if trades < 3 or tokens < 2:
        return 0
    wr_score = winrate * 100
    profit_score = min(profit / 100, 50)
    consistency = ((pnl_2x + pnl_5x * 2) / max(tokens, 1)) * 30
    size_bonus = min(tokens / 10, 10)
    return round(wr_score + profit_score + consistency + size_bonus, 2)


def _calc_tier(score: float, winrate: float, pnl_5x: int) -> str:
    if score >= 150 and winrate >= 0.70 and pnl_5x >= 2:
        return "S"
    if score >= 100 and winrate >= 0.60:
        return "A"
    if score >= 60:
        return "B"
    if score > 0:
        return "C"
    return "NEW"


# === AUTO-DISCOVERY ===

async def _auto_discover_wallets():
    """Find new high-performing wallets from GMGN smart money data."""
    from ..services.gmgn_client import get_smart_money_trades
    data = await get_smart_money_trades()
    if True:  # keep indentation
        if not data:
            return

        items = data if isinstance(data, list) else data.get("data", data)
        if isinstance(items, dict):
            items = items.get("list", [])
        if not isinstance(items, list):
            return

    # Extract unique wallets
    wallets = set()
    wallet_info = {}
    for t in items:
        addr = t.get("maker", "")
        if not addr:
            continue
        wallets.add(addr)
        mi = t.get("maker_info", {})
        wallet_info[addr] = {
            "twitter": mi.get("twitter_username", ""),
            "name": mi.get("name") or mi.get("twitter_name", ""),
            "tags": ",".join(mi.get("tags", [])),
        }

    async with async_session() as db:
        existing = set((await db.execute(
            select(FollowedWallet.wallet)
        )).scalars().all())

        added = 0
        for addr in wallets:
            if addr in existing:
                continue
            info = wallet_info.get(addr, {})
            fw = FollowedWallet(
                wallet=addr,
                label=info.get("name", ""),
                source="auto_discovered",
                twitter=info.get("twitter", ""),
                tags=info.get("tags", ""),
                auto_discovered=True,
            )
            db.add(fw)
            existing.add(addr)
            added += 1

        if added:
            await db.commit()
            _log.info(f"Auto-discovered {added} new wallets from GMGN smart money")


# === TRADE MONITORING ===

async def _poll_wallet_trades():
    """Check followed wallets for new trades via GMGN."""
    async with async_session() as db:
        # Get active followed wallets, prioritize highest scores
        wallets = (await db.execute(
            select(FollowedWallet)
            .where(FollowedWallet.active == True)
            .order_by(FollowedWallet.score.desc().nullslast())
            .limit(50)
        )).scalars().all()

    if not wallets:
        return

    wallet_addrs = [w.wallet for w in wallets]
    wallet_map = {w.wallet: w for w in wallets}

    from ..services.gmgn_client import get_wallet_activities
    if True:  # keep indentation
        for wallet in wallets[:20]:
            data = await get_wallet_activities(wallet.wallet)
            if not data:
                continue

            activities = data.get("data", data)
            if isinstance(activities, dict):
                activities = activities.get("list", activities.get("activities", []))
            if not isinstance(activities, list):
                await asyncio.sleep(0.5)
                continue

            async with async_session() as db:
                for act in activities:
                    tx_hash = act.get("tx_hash") or act.get("transaction_hash", "")
                    if not tx_hash:
                        continue

                    # Check if we already have this trade
                    existing = (await db.execute(
                        select(WalletTrade).where(WalletTrade.tx_hash == tx_hash)
                    )).scalar_one_or_none()
                    if existing:
                        continue

                    side = act.get("side") or act.get("event_type", "")
                    if side not in ("buy", "sell"):
                        continue

                    token_addr = act.get("token_address") or act.get("base_address", "")
                    symbol = act.get("token_symbol") or (act.get("base_token", {}) or {}).get("symbol", "")
                    amount_usd = float(act.get("amount_usd", 0) or act.get("cost_usd", 0) or 0)
                    price = float(act.get("price_usd", 0) or act.get("price", 0) or 0)
                    ts = act.get("timestamp") or act.get("block_timestamp")
                    trade_time = datetime.utcfromtimestamp(ts) if ts and isinstance(ts, (int, float)) else datetime.utcnow()

                    trade = WalletTrade(
                        wallet=wallet.wallet,
                        tx_hash=tx_hash,
                        token_address=token_addr,
                        token_symbol=symbol[:20] if symbol else "",
                        side=side,
                        amount_usd=Decimal(str(amount_usd)),
                        price_usd=Decimal(str(price)) if price else None,
                        trade_time=trade_time,
                    )
                    db.add(trade)

                    if side == "buy" and amount_usd >= 50:
                        _log.info(f"FOLLOWED WALLET BUY: {wallet.label or wallet.wallet[:12]}... bought ${symbol} for ${amount_usd:,.0f}")

                await db.commit()

            await asyncio.sleep(1)

    # Run cluster detection
    await _detect_follow_clusters()


# === CLUSTER DETECTION ===

async def _detect_follow_clusters():
    """Detect when multiple followed wallets buy the same token."""
    cutoff = datetime.utcnow() - timedelta(minutes=60)

    async with async_session() as db:
        recent_buys = (await db.execute(
            select(WalletTrade)
            .where(WalletTrade.side == "buy", WalletTrade.trade_time >= cutoff)
            .order_by(WalletTrade.trade_time.desc())
        )).scalars().all()

        clusters = defaultdict(list)
        for t in recent_buys:
            clusters[t.token_address].append(t)

        import json as _json
        for token_addr, trades in clusters.items():
            unique_wallets = set(t.wallet for t in trades)
            if len(unique_wallets) < 2:
                continue

            # Check for existing signal
            existing = (await db.execute(
                select(WalletSignal)
                .where(WalletSignal.token_address == token_addr,
                       WalletSignal.detected_at >= cutoff)
            )).scalar_one_or_none()
            if existing:
                continue

            # Get wallet scores
            scores = []
            wallet_details = []
            for addr in unique_wallets:
                w = (await db.execute(
                    select(FollowedWallet).where(FollowedWallet.wallet == addr)
                )).scalar_one_or_none()
                if w:
                    scores.append(float(w.score or 0))
                    wallet_details.append({
                        "wallet": addr[:12] + "...",
                        "label": w.label or "",
                        "twitter": w.twitter or "",
                        "tier": w.tier,
                        "score": float(w.score or 0),
                    })

            avg_score = sum(scores) / len(scores) if scores else 0
            total_usd = sum(float(t.amount_usd or 0) for t in trades)
            symbol = trades[0].token_symbol or ""

            if len(unique_wallets) >= 4 or (len(unique_wallets) >= 3 and avg_score >= 80):
                strength = "VERY_STRONG"
            elif len(unique_wallets) >= 3 or (len(unique_wallets) >= 2 and avg_score >= 100):
                strength = "STRONG"
            else:
                strength = "MEDIUM"

            labels = [d.get("label") or d.get("twitter") or d["wallet"] for d in wallet_details]
            desc = f"${symbol}: {len(unique_wallets)} followed wallets buying ({', '.join(labels[:4])}). ${total_usd:,.0f} total."

            signal = WalletSignal(
                token_address=token_addr, token_symbol=symbol,
                wallet_count=len(unique_wallets),
                total_usd=Decimal(str(total_usd)),
                avg_wallet_score=Decimal(str(round(avg_score, 2))),
                strength=strength,
                wallets_json=_json.dumps(wallet_details),
                description=desc,
            )
            db.add(signal)

            _log.warning(f"FOLLOW SIGNAL [{strength}]: ${symbol} — {desc[:100]}")

            from ..models.platform import Notification
            db.add(Notification(
                agent_id="0xb18a31796ea51c52c203c96aab0b1bc551c4e051",
                type="wallet_follow_signal",
                title=f"Followed Wallets [{strength}]: ${symbol}",
                body=desc[:200], link="/trading.html",
            ))

        await db.commit()


# === PERFORMANCE TRACKING ===

async def _refresh_wallet_stats():
    """Update wallet PNL stats from GMGN and prune bad performers."""
    async with async_session() as db:
        wallets = (await db.execute(
            select(FollowedWallet)
            .where(FollowedWallet.active == True)
            .where((FollowedWallet.last_stats_update.is_(None)) |
                   (FollowedWallet.last_stats_update < datetime.utcnow() - timedelta(hours=2)))
            .order_by(FollowedWallet.last_stats_update.asc().nullsfirst())
            .limit(20)
        )).scalars().all()

    if not wallets:
        return

    _log.info(f"Refreshing stats for {len(wallets)} followed wallets")

    from ..services.gmgn_client import get_wallet_stats
    if True:  # keep indentation
        for w in wallets:
            data = await get_wallet_stats(w.wallet, period="7d")
            if not data:
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

            async with async_session() as db2:
                wallet = (await db2.execute(
                    select(FollowedWallet).where(FollowedWallet.wallet == w.wallet)
                )).scalar_one_or_none()
                if not wallet:
                    continue

                wallet.winrate = Decimal(str(round(winrate, 4)))
                wallet.realized_profit = Decimal(str(realized))
                wallet.total_trades = buy_count + sell_count
                wallet.tokens_traded = tokens
                wallet.pnl_2x_plus = pnl_2x
                wallet.pnl_5x_plus = pnl_5x
                wallet.twitter = common.get("twitter_username") or wallet.twitter
                wallet.label = common.get("name") or common.get("twitter_name") or wallet.label

                score = _calc_wallet_score(winrate, realized, buy_count + sell_count, pnl_2x, pnl_5x, tokens)
                tier = _calc_tier(score, winrate, pnl_5x)

                wallet.score = Decimal(str(score))
                wallet.tier = tier
                wallet.last_stats_update = datetime.utcnow()

                # Auto-prune: deactivate consistently bad auto-discovered wallets
                if wallet.auto_discovered and score < MIN_SCORE_TO_KEEP and tokens >= 10:
                    wallet.active = False
                    _log.info(f"Pruned wallet {wallet.wallet[:12]}... score={score}, WR={winrate:.0%}")

                await db2.commit()

            await asyncio.sleep(1.5)


# === API FOR ADDING WALLETS ===

async def add_wallet(wallet_addr: str, label: str = "", source: str = "manual",
                     twitter: str = "", tags: str = ""):
    """Add a wallet to the follow list."""
    async with async_session() as db:
        existing = (await db.execute(
            select(FollowedWallet).where(FollowedWallet.wallet == wallet_addr)
        )).scalar_one_or_none()
        if existing:
            if not existing.active:
                existing.active = True
                await db.commit()
            return existing

        fw = FollowedWallet(
            wallet=wallet_addr, label=label, source=source,
            twitter=twitter, tags=tags,
        )
        db.add(fw)
        await db.commit()
        _log.info(f"Added followed wallet: {label or wallet_addr[:12]}... (source: {source})")
        return fw


# === MAIN LOOP ===

async def run():
    _log.info("Wallet Follow tracker starting — monitoring followed wallets")
    await asyncio.sleep(35)
    cycle = 0
    last_stats = 0
    last_discover = 0

    while True:
        try:
            # Poll trades every cycle
            await _poll_wallet_trades()

            # Auto-discover new wallets every 30 min
            if time.time() - last_discover > 1800:
                await _auto_discover_wallets()
                last_discover = time.time()

            # Refresh stats every 2 hours
            if time.time() - last_stats > STATS_REFRESH_INTERVAL:
                await _refresh_wallet_stats()
                last_stats = time.time()

            cycle += 1
        except Exception as e:
            _log.error(f"Wallet follow error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run())

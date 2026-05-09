# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""
Correlation Engine — the core alpha logic.
Only fires a signal when multiple independent data sources agree on the same token.
Trigger → Filter → Audit → Signal
"""
import asyncio
import logging
import json as _json
from datetime import datetime, timedelta
from decimal import Decimal

import os
import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("correlation")

POLL_INTERVAL = 30
LOOKBACK_MINUTES = 60
SOURCE_LOOKBACK_HOURS = 6
MIN_SOURCES = 2
MIN_MC = 100_000

LUNARCRUSH_KEY = os.getenv("LUNARCRUSH_API_KEY", "")
LUNARCRUSH_BASE = "https://lunarcrush.com/api4/public"


class CorrelatedSignal(Base):
    __tablename__ = "correlated_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(String(66), nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sources_json: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(30), nullable=False)
    mc_at_signal: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    price_at_signal: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    price_1h: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    price_6h: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    price_24h: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    pct_change_1h: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    pct_change_6h: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    pct_change_24h: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_corr_time", "detected_at"),
        Index("idx_corr_token", "token_address"),
        Index("idx_corr_confidence", "confidence"),
    )


async def _get_lunarcrush(symbol: str) -> dict | None:
    """Get LunarCrush Galaxy Score, AltRank, and sentiment for a coin."""
    if not LUNARCRUSH_KEY:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{LUNARCRUSH_BASE}/coins/{symbol}/v1",
                                    headers={"Authorization": f"Bearer {LUNARCRUSH_KEY}"}, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return {
                    "galaxy_score": data.get("galaxy_score"),
                    "alt_rank": data.get("alt_rank"),
                    "sentiment": data.get("sentiment"),
                    "volatility": data.get("volatility"),
                    "market_cap_rank": data.get("market_cap_rank"),
                    "percent_change_24h": data.get("percent_change_24h"),
                }
    except Exception:
        pass
    return None


async def _get_mc_and_price(token_addr: str) -> tuple:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.dexscreener.com/token-pairs/v1/solana/{token_addr}", timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if pairs:
                    return float(pairs[0].get("fdv", 0) or 0), float(pairs[0].get("priceUsd", 0) or 0)
    except Exception:
        pass
    return 0, 0


async def _correlate():
    """Main correlation logic: Trigger → Filter → Audit → Signal."""
    async with async_session() as db:
        trigger_cutoff = datetime.utcnow() - timedelta(minutes=LOOKBACK_MINUTES)
        source_cutoff = datetime.utcnow() - timedelta(hours=SOURCE_LOOKBACK_HOURS)

        # === STEP A: TRIGGER — Get candidate tokens from cluster signals ===
        from .smart_money_tracker import ClusterSignal
        candidates = (await db.execute(
            select(ClusterSignal)
            .where(ClusterSignal.detected_at >= trigger_cutoff)
            .order_by(ClusterSignal.detected_at.desc())
        )).scalars().all()

        # Deduplicate by token address
        seen_tokens = {}
        for c in candidates:
            if c.token_address not in seen_tokens:
                seen_tokens[c.token_address] = c

        if not seen_tokens:
            return

        for token_addr, cluster in seen_tokens.items():
            symbol = cluster.token_symbol or ""

            # Skip if we already created a correlated signal for this token recently
            existing = (await db.execute(
                select(CorrelatedSignal)
                .where(CorrelatedSignal.token_address == token_addr,
                       CorrelatedSignal.detected_at >= trigger_cutoff)
            )).scalar_one_or_none()
            if existing:
                continue

            # Check MC
            mc, price = await _get_mc_and_price(token_addr)
            if mc < MIN_MC:
                continue

            sources = []
            confidence = 0

            # Source 1: Smart Money Cluster (already confirmed — it's the trigger)
            cluster_strength = cluster.signal_strength
            cluster_bonus = {"VERY_STRONG": 15, "STRONG": 10, "MEDIUM": 5}.get(cluster_strength, 5)
            sources.append({
                "source": "smart_money",
                "detail": f"{cluster.wallet_count} wallets, ${float(cluster.total_usd or 0):,.0f}",
                "strength": cluster_strength,
            })
            confidence += 20 + cluster_bonus

            # === STEP B: FILTER — Check sentiment ===
            from .sentiment_tracker import SocialMention
            sent_result = (await db.execute(
                select(
                    func.count(func.distinct(SocialMention.platform)).label("platforms"),
                    func.sum(SocialMention.mention_count).label("mentions"),
                    func.avg(SocialMention.sentiment_score).label("avg_score"),
                )
                .where(SocialMention.token_symbol == symbol.upper(),
                       SocialMention.detected_at >= source_cutoff)
            )).first()

            if sent_result and sent_result.platforms and int(sent_result.platforms) >= 2:
                avg_sent = float(sent_result.avg_score or 0)
                sources.append({
                    "source": "social",
                    "detail": f"{int(sent_result.mentions or 0)} mentions, {int(sent_result.platforms)} platforms, score {avg_sent:.0f}",
                    "platforms": int(sent_result.platforms),
                })
                confidence += 20 + min(int(abs(avg_sent)), 15)

            # === STEP C: AUDIT — Check deployer ===
            from ..models.platform import MemeDeployment, TopDeployer
            deployment = (await db.execute(
                select(MemeDeployment).where(MemeDeployment.mint_address == token_addr)
            )).scalar_one_or_none()

            if deployment and deployment.deployer_wallet:
                top_deployer = (await db.execute(
                    select(TopDeployer).where(TopDeployer.wallet == deployment.deployer_wallet)
                )).scalar_one_or_none()

                if top_deployer and top_deployer.rating != "D":
                    deployer_bonus = {"S": 15, "A": 10, "B": 5, "C": 3}.get(top_deployer.rating, 0)
                    rug_penalty = min(int(top_deployer.rug_count or 0) * 10, 20)
                    sources.append({
                        "source": "deployer",
                        "detail": f"Rating {top_deployer.rating}, {top_deployer.tokens_over_1m} hits, {top_deployer.rug_count} rugs",
                        "rating": top_deployer.rating,
                    })
                    confidence += 20 + deployer_bonus - rug_penalty

            # === AUDIT — Check followed wallets ===
            from .wallet_follow import WalletTrade
            follow_result = (await db.execute(
                select(
                    func.count(func.distinct(WalletTrade.wallet)).label("wallets"),
                    func.sum(WalletTrade.amount_usd).label("total_usd"),
                )
                .where(WalletTrade.token_address == token_addr,
                       WalletTrade.side == "buy",
                       WalletTrade.trade_time >= source_cutoff)
            )).first()

            if follow_result and follow_result.wallets and int(follow_result.wallets) >= 1:
                fw_count = int(follow_result.wallets)
                fw_bonus = 10 if fw_count >= 3 else 5 if fw_count >= 2 else 3
                sources.append({
                    "source": "followed_wallets",
                    "detail": f"{fw_count} wallets, ${float(follow_result.total_usd or 0):,.0f}",
                    "wallets": fw_count,
                })
                confidence += 20 + fw_bonus

            # === AUDIT — Check whale activity ===
            from .whale_tracker import WhaleTransaction
            whale_result = (await db.execute(
                select(func.count(), func.sum(WhaleTransaction.amount_usd))
                .where(WhaleTransaction.symbol == symbol.upper(),
                       WhaleTransaction.trade_time >= source_cutoff,
                       WhaleTransaction.tx_type.in_(["exchange_withdrawal", "whale_transfer"]))
            )).first()

            if whale_result and whale_result[0] and int(whale_result[0]) >= 1:
                whale_usd = float(whale_result[1] or 0)
                whale_bonus = 10 if whale_usd >= 10_000_000 else 5 if whale_usd >= 1_000_000 else 2
                sources.append({
                    "source": "whale_flow",
                    "detail": f"{int(whale_result[0])} transfers, ${whale_usd:,.0f} (accumulation)",
                })
                confidence += 20 + whale_bonus

            # === AUDIT — LunarCrush Galaxy Score & Sentiment ===
            lc = await _get_lunarcrush(symbol.upper())
            if lc and lc.get("galaxy_score"):
                gs = float(lc["galaxy_score"])
                alt_rank = int(lc.get("alt_rank") or 9999)
                lc_sentiment = int(lc.get("sentiment") or 50)
                # Galaxy Score > 60 = bullish social momentum
                if gs >= 50:
                    lc_bonus = 15 if gs >= 70 else 10 if gs >= 60 else 5
                    # AltRank boost — lower rank = more attention
                    rank_bonus = 5 if alt_rank <= 20 else 3 if alt_rank <= 50 else 0
                    sources.append({
                        "source": "lunarcrush",
                        "detail": f"Galaxy Score {gs:.1f}, AltRank #{alt_rank}, Sentiment {lc_sentiment}",
                        "galaxy_score": gs,
                        "alt_rank": alt_rank,
                        "sentiment": lc_sentiment,
                    })
                    confidence += 20 + lc_bonus + rank_bonus

            # === STEP D: SIGNAL — Only if 2+ sources agree ===
            if len(sources) < MIN_SOURCES:
                continue

            confidence = max(0, min(100, confidence))

            signal = CorrelatedSignal(
                token_address=token_addr,
                token_symbol=symbol,
                confidence=confidence,
                source_count=len(sources),
                sources_json=_json.dumps(sources),
                trigger_source="smart_money_cluster",
                mc_at_signal=Decimal(str(mc)),
                price_at_signal=Decimal(str(price)) if price else None,
            )
            db.add(signal)

            source_names = [s["source"] for s in sources]
            _log.warning(
                f"CORRELATED SIGNAL [confidence={confidence}]: ${symbol} MC=${mc:,.0f} — "
                f"{len(sources)} sources: {', '.join(source_names)}"
            )

            from ..models.platform import Notification
            db.add(Notification(
                agent_id="0xb18a31796ea51c52c203c96aab0b1bc551c4e051",
                type="correlated_signal",
                title=f"Alpha Signal [{confidence}/100]: ${symbol}",
                body=f"{len(sources)} sources agree: {', '.join(source_names)}. MC: ${mc:,.0f}",
                link="/trading.html",
            ))

        await db.commit()


async def _check_accuracy():
    """Check price performance of correlated signals at 1h, 6h, 24h."""
    async with async_session() as db:
        now = datetime.utcnow()

        checks = [
            ("1h", 1, 2, "price_1h", "pct_change_1h"),
            ("6h", 6, 7, "price_6h", "pct_change_6h"),
            ("24h", 24, 25, "price_24h", "pct_change_24h"),
        ]

        for label, min_h, max_h, price_col, pct_col in checks:
            signals = (await db.execute(
                select(CorrelatedSignal)
                .where(CorrelatedSignal.price_at_signal.isnot(None),
                       getattr(CorrelatedSignal, price_col).is_(None),
                       CorrelatedSignal.detected_at <= now - timedelta(hours=min_h),
                       CorrelatedSignal.detected_at >= now - timedelta(hours=max_h))
                .limit(10)
            )).scalars().all()

            for s in signals:
                mc, current_price = await _get_mc_and_price(s.token_address)
                signal_price = float(s.price_at_signal or 0)

                if signal_price > 0 and current_price > 0:
                    pct = (current_price - signal_price) / signal_price
                    setattr(s, price_col, Decimal(str(current_price)))
                    setattr(s, pct_col, Decimal(str(round(pct, 4))))

                    if label == "24h":
                        if pct >= 0.5:
                            s.outcome = "BIG_WIN"
                        elif pct >= 0.1:
                            s.outcome = "WIN"
                        elif pct >= -0.1:
                            s.outcome = "NEUTRAL"
                        elif pct >= -0.5:
                            s.outcome = "LOSS"
                        else:
                            s.outcome = "BIG_LOSS"
                        _log.info(f"Correlated signal {s.token_symbol}: 24h {pct:+.1%} -> {s.outcome} (confidence={s.confidence})")

                await asyncio.sleep(0.5)

        await db.commit()


async def run():
    _log.info("Correlation Engine starting — Trigger → Filter → Audit → Signal")
    await asyncio.sleep(40)
    cycle = 0

    while True:
        try:
            await _correlate()

            if cycle % 10 == 0:
                await _check_accuracy()

            cycle += 1
        except Exception as e:
            _log.error(f"Correlation engine error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

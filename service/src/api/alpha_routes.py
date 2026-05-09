# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Alpha API — one endpoint, one answer. The product."""
import os
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from ..core.database import get_db

router = APIRouter(prefix="/v1/alpha", tags=["alpha"])

LUNARCRUSH_KEY = os.getenv("LUNARCRUSH_API_KEY", "")


@router.get("/{token}")
async def get_alpha(token: str, db: AsyncSession = Depends(get_db)):
    """One call, one answer. Returns BUY/SELL/HOLD with confidence and sources."""
    token = token.upper()
    confidence = 0
    sources = []
    details = {}

    # 1. Check correlated signals (highest value — multiple sources already agreed)
    from ..workers.correlation_engine import CorrelatedSignal
    corr = (await db.execute(
        select(CorrelatedSignal)
        .where(CorrelatedSignal.token_symbol == token,
               CorrelatedSignal.detected_at >= datetime.utcnow() - timedelta(hours=24))
        .order_by(CorrelatedSignal.confidence.desc())
        .limit(1)
    )).scalar_one_or_none()

    if corr:
        corr_boost = min(int(corr.confidence * 0.3), 25)
        confidence += corr_boost
        sources.append("correlated")
        details["correlated"] = {
            "confidence": corr.confidence,
            "source_count": corr.source_count,
            "detected_at": corr.detected_at.isoformat(),
        }

    # 2. Check smart money clusters
    from ..workers.smart_money_tracker import ClusterSignal
    cluster = (await db.execute(
        select(ClusterSignal)
        .where(ClusterSignal.token_symbol == token,
               ClusterSignal.detected_at >= datetime.utcnow() - timedelta(hours=24))
        .order_by(ClusterSignal.detected_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    if cluster:
        cl_boost = {"VERY_STRONG": 20, "STRONG": 15, "MEDIUM": 10}.get(cluster.signal_strength, 5)
        confidence += cl_boost
        sources.append("smart_money")
        details["smart_money"] = {
            "wallets": cluster.wallet_count,
            "total_usd": float(cluster.total_usd or 0),
            "strength": cluster.signal_strength,
        }

    # 3. Check whale flow
    from ..workers.whale_tracker import WhaleTransaction
    cutoff_6h = datetime.utcnow() - timedelta(hours=6)
    deposits = (await db.execute(
        select(func.sum(WhaleTransaction.amount_usd))
        .where(WhaleTransaction.symbol == token,
               WhaleTransaction.trade_time >= cutoff_6h,
               WhaleTransaction.tx_type == "exchange_deposit")
    )).scalar() or 0
    withdrawals = (await db.execute(
        select(func.sum(WhaleTransaction.amount_usd))
        .where(WhaleTransaction.symbol == token,
               WhaleTransaction.trade_time >= cutoff_6h,
               WhaleTransaction.tx_type == "exchange_withdrawal")
    )).scalar() or 0

    deposit_val = float(deposits)
    withdrawal_val = float(withdrawals)
    net_flow = deposit_val - withdrawal_val
    whale_flow = "NONE"

    if deposit_val + withdrawal_val > 0:
        if net_flow > 1_000_000:
            whale_flow = "SELL_PRESSURE"
            confidence -= 15
            sources.append("whale_sell_pressure")
        elif net_flow < -1_000_000:
            whale_flow = "ACCUMULATION"
            confidence += 15
            sources.append("whale_accumulation")
        else:
            whale_flow = "MIXED"
        details["whale_flow"] = {
            "direction": whale_flow,
            "deposits_usd": deposit_val,
            "withdrawals_usd": withdrawal_val,
            "net_usd": net_flow,
        }

    # 4. Check social sentiment
    from ..workers.sentiment_tracker import SocialMention
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    sent_result = (await db.execute(
        select(
            func.count().label("cnt"),
            func.count(func.distinct(SocialMention.platform)).label("platforms"),
            func.avg(SocialMention.sentiment_score).label("avg_score"),
        )
        .where(SocialMention.token_symbol == token,
               SocialMention.detected_at >= cutoff_24h)
    )).first()

    mention_count = int(sent_result.cnt or 0)
    if mention_count > 0:
        avg_sent = float(sent_result.avg_score or 0)
        platforms = int(sent_result.platforms or 0)
        if mention_count >= 50:
            confidence += 15
        elif mention_count >= 10:
            confidence += 8
        if avg_sent > 10:
            confidence += 5
        elif avg_sent < -10:
            confidence -= 5
        sources.append("social")
        details["social"] = {
            "mentions": mention_count,
            "platforms": platforms,
            "avg_sentiment_score": round(avg_sent, 1),
        }

    # 5. LunarCrush
    galaxy_score = None
    alt_rank = None
    if LUNARCRUSH_KEY:
        try:
            async with httpx.AsyncClient() as client:
                lc_resp = await client.get(
                    f"https://lunarcrush.com/api4/public/coins/{token}/v1",
                    headers={"Authorization": f"Bearer {LUNARCRUSH_KEY}"}, timeout=8)
                if lc_resp.status_code == 200:
                    lc = lc_resp.json().get("data", {})
                    galaxy_score = lc.get("galaxy_score")
                    alt_rank = lc.get("alt_rank")
                    if galaxy_score and float(galaxy_score) >= 60:
                        confidence += 10
                        sources.append("lunarcrush")
                    details["lunarcrush"] = {
                        "galaxy_score": galaxy_score,
                        "alt_rank": alt_rank,
                        "percent_change_24h": lc.get("percent_change_24h"),
                    }
        except Exception:
            pass

    # 6. Check stock insider/Congress data
    from ..workers.stocks_tracker import StockWhaleMove
    buy_keywords = {"purchase", "buy", "acquired"}
    sell_keywords = {"sale", "sell", "sold", "disposition"}

    insider_moves = (await db.execute(
        select(StockWhaleMove)
        .where(StockWhaleMove.ticker == token,
               StockWhaleMove.filing_date >= datetime.utcnow() - timedelta(days=30))
        .order_by(StockWhaleMove.filing_date.desc())
        .limit(20)
    )).scalars().all()

    if insider_moves:
        insider_buys = [m for m in insider_moves if any(k in (m.action or "").lower() for k in buy_keywords)]
        insider_sells = [m for m in insider_moves if any(k in (m.action or "").lower() for k in sell_keywords)]
        congress_moves = [m for m in insider_moves if m.source == "congress"]

        buy_score = len(insider_buys) * 3 + len([m for m in congress_moves if any(k in (m.action or "").lower() for k in buy_keywords)]) * 5
        sell_score = len(insider_sells) * 3 + len([m for m in congress_moves if any(k in (m.action or "").lower() for k in sell_keywords)]) * 5

        if buy_score > sell_score:
            confidence += min(buy_score * 2, 25)
            sources.append("insider_buying")
        elif sell_score > buy_score:
            confidence -= min(sell_score * 2, 25)
            sources.append("insider_selling")

        recent_trades = []
        for m in insider_moves[:5]:
            recent_trades.append({
                "filer": m.filer_name,
                "action": m.action,
                "date": m.filing_date.strftime("%Y-%m-%d"),
                "shares": float(m.shares) if m.shares else None,
                "value": float(m.value_usd) if m.value_usd else None,
                "price": round(float(m.value_usd) / float(m.shares), 2) if m.value_usd and m.shares and float(m.shares) > 0 else None,
                "source": m.source,
            })

        details["insider_data"] = {
            "total_moves": len(insider_moves),
            "insider_buys": len(insider_buys),
            "insider_sells": len(insider_sells),
            "congress_moves": len(congress_moves),
            "recent_trades": recent_trades,
        }

    # 6b. Deployer Rating (Agiotage exclusive)
    from ..models.platform import MemeDeployment, TopDeployer
    deployer_rating = None
    deployer_data = {}
    token_deployment = (await db.execute(
        select(MemeDeployment).where(
            (MemeDeployment.token_symbol == token) | (MemeDeployment.mint_address == token)
        ).limit(1)
    )).scalar_one_or_none()

    if token_deployment and token_deployment.deployer_wallet:
        top_dep = (await db.execute(
            select(TopDeployer).where(TopDeployer.wallet == token_deployment.deployer_wallet)
        )).scalar_one_or_none()
        if top_dep:
            deployer_rating = top_dep.rating
            deployer_data = {
                "rating": top_dep.rating,
                "total_tokens": top_dep.total_tokens,
                "tokens_over_1m": top_dep.tokens_over_1m,
                "rug_count": top_dep.rug_count,
                "highest_mc": float(top_dep.highest_mc or 0),
                "avg_peak_mc": float(top_dep.avg_peak_mc or 0),
            }
            # Boost/penalize based on rating
            if deployer_rating == "S":
                confidence += 20
                sources.append("deployer_S_tier")
            elif deployer_rating == "A":
                confidence += 15
                sources.append("deployer_A_tier")
            elif deployer_rating == "B":
                confidence += 10
                sources.append("deployer_rated")
            elif deployer_rating == "D":
                confidence -= 20
                sources.append("deployer_rug_warning")
            details["deployer_rating"] = deployer_data

    # 7. Token Security Audit (GMGN — meme/crypto tokens)
    gmgn_key = os.getenv("GMGN_API_KEY", "")
    if gmgn_key and token not in ("BTC", "ETH", "SOL", "XRP", "LINK", "AVAX", "DOT", "ADA", "BNB"):
        import time as _time, uuid as _uuid
        try:
            async with httpx.AsyncClient() as client:
                # Security check
                sec_resp = await client.get(f"https://openapi.gmgn.ai/v1/token/security",
                    params={"chain": "sol", "address": token, "timestamp": int(_time.time()), "client_id": str(_uuid.uuid4())},
                    headers={"X-APIKEY": gmgn_key}, timeout=10)
                if sec_resp.status_code == 200:
                    sec = sec_resp.json().get("data", {})
                    rug_ratio = sec.get("rug_ratio", 0)
                    is_honeypot = sec.get("is_honeypot", "")
                    renounced_mint = sec.get("renounced_mint", False)
                    renounced_freeze = sec.get("renounced_freeze_account", False)
                    top_10_rate = sec.get("top_10_holder_rate", 0)
                    creator_status = sec.get("creator_token_status", "")
                    buy_tax = sec.get("buy_tax", 0)
                    sell_tax = sec.get("sell_tax", 0)

                    # Score safety
                    safety_score = 100
                    safety_warnings = []
                    if is_honeypot == "yes":
                        safety_score = 0
                        safety_warnings.append("HONEYPOT DETECTED — DO NOT BUY")
                    if rug_ratio and float(rug_ratio) > 0.3:
                        safety_score -= 30
                        safety_warnings.append(f"High rug risk ({float(rug_ratio):.0%})")
                    if not renounced_mint:
                        safety_score -= 15
                        safety_warnings.append("Mint NOT renounced — supply can increase")
                    if not renounced_freeze:
                        safety_score -= 10
                        safety_warnings.append("Freeze NOT renounced — wallets can be frozen")
                    if top_10_rate and float(top_10_rate) > 0.5:
                        safety_score -= 15
                        safety_warnings.append(f"Top 10 holders own {float(top_10_rate):.0%}")
                    if buy_tax and float(buy_tax) > 0.05:
                        safety_score -= 10
                        safety_warnings.append(f"Buy tax: {float(buy_tax):.1%}")
                    if sell_tax and float(sell_tax) > 0.05:
                        safety_score -= 10
                        safety_warnings.append(f"Sell tax: {float(sell_tax):.1%}")

                    safety_score = max(0, safety_score)
                    safety_label = "SAFE" if safety_score >= 80 else "CAUTION" if safety_score >= 50 else "DANGER"

                    details["security"] = {
                        "safety_score": safety_score,
                        "safety_label": safety_label,
                        "warnings": safety_warnings,
                        "is_honeypot": is_honeypot,
                        "rug_ratio": float(rug_ratio) if rug_ratio else 0,
                        "renounced_mint": renounced_mint,
                        "renounced_freeze": renounced_freeze,
                        "top_10_holder_rate": float(top_10_rate) if top_10_rate else 0,
                        "creator_status": creator_status,
                        "buy_tax": float(buy_tax) if buy_tax else 0,
                        "sell_tax": float(sell_tax) if sell_tax else 0,
                    }

                    # Penalize confidence for unsafe tokens
                    if safety_score < 50:
                        confidence -= 20

                # Token info with deployer data
                info_resp = await client.get(f"https://openapi.gmgn.ai/v1/token/info",
                    params={"chain": "sol", "address": token, "timestamp": int(_time.time()), "client_id": str(_uuid.uuid4())},
                    headers={"X-APIKEY": gmgn_key}, timeout=10)
                if info_resp.status_code == 200:
                    info = info_resp.json().get("data", {})
                    dev = info.get("dev", {})
                    link = info.get("link", {})
                    stat = info.get("stat", {})
                    wallet_tags = info.get("wallet_tags_stat", {})

                    if not price:
                        price = float(info.get("price", 0) or 0)
                    if not mc:
                        circ = float(info.get("circulating_supply", 0) or 0)
                        if price and circ:
                            mc = price * circ

                    details["token_info"] = {
                        "holder_count": info.get("holder_count"),
                        "liquidity": float(info.get("liquidity", 0) or 0),
                        "launchpad": info.get("launchpad"),
                        "ath_price": info.get("ath_price"),
                        "creation_timestamp": info.get("creation_timestamp"),
                    }
                    details["deployer"] = {
                        "creator_address": dev.get("creator_address"),
                        "creator_status": dev.get("creator_token_status"),
                        "creator_balance": dev.get("creator_token_balance"),
                        "tokens_launched": dev.get("creator_open_count"),
                        "twitter": link.get("twitter_username"),
                        "website": link.get("website"),
                        "telegram": link.get("telegram"),
                    }
                    if dev.get("ath_token_info"):
                        ath = dev["ath_token_info"]
                        details["deployer"]["best_token"] = {
                            "symbol": ath.get("symbol"),
                            "ath_mc": ath.get("ath_mc"),
                        }
                    details["holder_breakdown"] = {
                        "smart_wallets": wallet_tags.get("smart_wallets", 0),
                        "kol_wallets": wallet_tags.get("renowned_wallets", 0),
                        "sniper_wallets": wallet_tags.get("sniper_wallets", 0),
                        "whale_wallets": wallet_tags.get("whale_wallets", 0),
                        "fresh_wallets": wallet_tags.get("fresh_wallets", 0),
                        "top_10_rate": float(stat.get("top_10_holder_rate", 0) or 0),
                        "dev_hold_rate": float(stat.get("dev_team_hold_rate", 0) or 0),
                    }

                # Top holders
                holders_resp = await client.get(f"https://openapi.gmgn.ai/v1/market/token_top_holders",
                    params={"chain": "sol", "address": token, "limit": 10,
                            "timestamp": int(_time.time()), "client_id": str(_uuid.uuid4())},
                    headers={"X-APIKEY": gmgn_key}, timeout=10)
                if holders_resp.status_code == 200:
                    holders_data = holders_resp.json().get("data", {})
                    holder_list = holders_data.get("list", []) if isinstance(holders_data, dict) else []
                    details["top_holders"] = [
                        {
                            "address": h.get("address", "")[:12] + "...",
                            "percentage": round(float(h.get("amount_percentage", 0) or 0) * 100, 2),
                            "value_usd": float(h.get("usd_value", 0) or 0),
                            "is_smart": "smart_degen" in (h.get("tags") or []),
                            "is_kol": "kol" in str(h.get("tags") or []),
                            "name": h.get("name") or h.get("twitter_name") or "",
                        }
                        for h in holder_list[:10]
                    ]
        except Exception:
            pass

    # 8. Get current price and MC
    price = None
    mc = None
    try:
        async with httpx.AsyncClient() as client:
            # Try CoinGecko for major coins
            coin_ids = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","XRP":"ripple","DOGE":"dogecoin",
                       "LINK":"chainlink","AVAX":"avalanche-2","ADA":"cardano","DOT":"polkadot","SUI":"sui"}
            cg_id = coin_ids.get(token)
            if cg_id:
                cg = await client.get(f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd&include_market_cap=true", timeout=5)
                if cg.status_code == 200:
                    d = cg.json().get(cg_id, {})
                    price = d.get("usd")
                    mc = d.get("usd_market_cap")
    except Exception:
        pass

    # Determine signal
    confidence = max(0, min(100, confidence))
    if confidence >= 60 and len(sources) >= 2:
        signal = "BUY"
    elif confidence <= 25 or "whale_sell_pressure" in sources:
        signal = "SELL" if confidence <= 15 else "HOLD"
    else:
        signal = "HOLD"

    # Build summary
    if signal == "BUY":
        summary = f"${token} shows BUY signal — Agiotage Score {confidence}/100 — {len(sources)} sources agree ({', '.join(sources)})"
    elif signal == "SELL":
        summary = f"${token} shows SELL pressure — Agiotage Score {confidence}/100 — {', '.join(sources)}"
    else:
        summary = f"${token} is HOLD — Agiotage Score {confidence}/100 — {'insufficient data' if not sources else f'{len(sources)} source(s): {chr(44).join(sources)}'}"

    return {
        "token": token,
        "signal": signal,
        "confidence": confidence,
        "agiotage_score": confidence,
        "sources": sources,
        "source_count": len(sources),
        "price": price,
        "market_cap": mc,
        "galaxy_score": galaxy_score,
        "alt_rank": alt_rank,
        "whale_flow": whale_flow,
        "details": details,
        "summary": summary,
        "generated_at": datetime.utcnow().isoformat(),
    }


@router.get("/scan/memes")
async def scan_memes(db: AsyncSession = Depends(get_db)):
    """Scan memecoins — pulls from cluster signals, deployers, and sentiment."""
    from ..workers.smart_money_tracker import ClusterSignal
    from ..models.platform import MemeDeployment, TopDeployer
    from ..workers.sentiment_tracker import SocialMention
    import httpx

    cutoff = datetime.utcnow() - timedelta(hours=6)

    # Get recent cluster signals with $100K+ MC
    clusters = (await db.execute(
        select(ClusterSignal)
        .where(ClusterSignal.detected_at >= cutoff)
        .order_by(ClusterSignal.detected_at.desc())
        .limit(50)
    )).scalars().all()

    # Deduplicate by token
    seen = {}
    for c in clusters:
        if c.token_symbol and c.token_symbol not in seen:
            seen[c.token_symbol] = c

    results = []
    for symbol, cluster in seen.items():
        # Get MC from DexScreener
        mc = 0
        price = 0
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.dexscreener.com/token-pairs/v1/solana/{cluster.token_address}", timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    pairs = data if isinstance(data, list) else data.get("pairs", [])
                    if pairs:
                        mc = float(pairs[0].get("fdv", 0) or 0)
                        price = float(pairs[0].get("priceUsd", 0) or 0)
        except:
            pass

        if mc < 100_000:
            continue

        # Check deployer
        deployer_rating = None
        deployment = (await db.execute(
            select(MemeDeployment).where(MemeDeployment.mint_address == cluster.token_address)
        )).scalar_one_or_none()
        if deployment and deployment.deployer_wallet:
            td = (await db.execute(
                select(TopDeployer).where(TopDeployer.wallet == deployment.deployer_wallet)
            )).scalar_one_or_none()
            if td:
                deployer_rating = td.rating

        # Check social
        mentions = (await db.execute(
            select(func.count()).select_from(SocialMention)
            .where(SocialMention.token_symbol == symbol, SocialMention.detected_at >= cutoff)
        )).scalar() or 0

        # Score
        confidence = 0
        sources = []

        # Cluster strength
        cl_boost = {"VERY_STRONG": 25, "STRONG": 20, "MEDIUM": 15}.get(cluster.signal_strength, 10)
        confidence += cl_boost
        sources.append("smart_money")

        # Deployer
        if deployer_rating and deployer_rating != "D":
            dep_boost = {"S": 20, "A": 15, "B": 10, "C": 5}.get(deployer_rating, 0)
            confidence += dep_boost
            sources.append("deployer")

        # Social
        if mentions >= 5:
            confidence += 15 if mentions >= 20 else 10
            sources.append("social")

        # Wallet count boost
        if cluster.wallet_count >= 5:
            confidence += 10
        elif cluster.wallet_count >= 4:
            confidence += 5

        confidence = min(100, confidence)

        signal = "BUY" if confidence >= 50 and len(sources) >= 2 else "HOLD" if confidence >= 25 else "SELL"

        results.append({
            "token": symbol,
            "token_address": cluster.token_address,
            "signal": signal,
            "confidence": confidence,
            "sources": sources,
            "source_count": len(sources),
            "market_cap": mc,
            "price": price,
            "wallet_count": cluster.wallet_count,
            "cluster_strength": cluster.signal_strength,
            "deployer_rating": deployer_rating,
            "social_mentions": mentions,
            "detected_at": cluster.detected_at.isoformat(),
        })

    results.sort(key=lambda x: x["confidence"], reverse=True)
    return {"count": len(results), "memes": results[:20], "generated_at": datetime.utcnow().isoformat()}


@router.get("/accuracy")
async def signal_accuracy(db: AsyncSession = Depends(get_db)):
    """Public accuracy dashboard — proves our signals work."""
    from ..workers.smart_money_tracker import ClusterSignal
    from ..workers.correlation_engine import CorrelatedSignal

    # Cluster signal accuracy
    cluster_scored = (await db.execute(
        select(func.count()).select_from(ClusterSignal).where(ClusterSignal.outcome.isnot(None))
    )).scalar() or 0
    cluster_wins = (await db.execute(
        select(func.count()).select_from(ClusterSignal).where(ClusterSignal.outcome.in_(["WIN", "BIG_WIN"]))
    )).scalar() or 0

    # Correlated signal accuracy
    corr_scored = (await db.execute(
        select(func.count()).select_from(CorrelatedSignal).where(CorrelatedSignal.outcome.isnot(None))
    )).scalar() or 0
    corr_wins = (await db.execute(
        select(func.count()).select_from(CorrelatedSignal).where(CorrelatedSignal.outcome.in_(["WIN", "BIG_WIN"]))
    )).scalar() or 0

    # Average changes by strength
    strong_avg = (await db.execute(
        select(func.avg(ClusterSignal.pct_change_24h))
        .where(ClusterSignal.signal_strength.in_(["STRONG", "VERY_STRONG"]),
               ClusterSignal.pct_change_24h.isnot(None))
    )).scalar()
    medium_avg = (await db.execute(
        select(func.avg(ClusterSignal.pct_change_24h))
        .where(ClusterSignal.signal_strength == "MEDIUM",
               ClusterSignal.pct_change_24h.isnot(None))
    )).scalar()

    return {
        "cluster_signals": {
            "scored": cluster_scored,
            "wins": cluster_wins,
            "win_rate": round(cluster_wins / max(cluster_scored, 1) * 100, 1),
        },
        "correlated_signals": {
            "scored": corr_scored,
            "wins": corr_wins,
            "win_rate": round(corr_wins / max(corr_scored, 1) * 100, 1),
        },
        "avg_24h_return": {
            "strong_signals": round(float(strong_avg or 0) * 100, 1),
            "medium_signals": round(float(medium_avg or 0) * 100, 1),
        },
        "note": "Win = price up 10%+ within 24 hours of signal. Tracked automatically.",
    }


@router.get("/scan/stocks")
async def scan_stocks(db: AsyncSession = Depends(get_db)):
    """Scan stocks — Agiotage Score combining insider, Congress, options flow, and social."""
    import httpx, os

    tickers = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD", "PLTR",
               "COIN", "HOOD", "SOFI", "NFLX", "CRM", "SHOP", "SQ", "MSTR", "SMCI", "ARM"]

    from ..workers.stocks_tracker import StockWhaleMove
    cutoff_30d = datetime.utcnow() - timedelta(days=30)
    buy_keywords = {"purchase", "buy", "acquired"}
    sell_keywords = {"sale", "sell", "sold", "disposition"}

    results = []
    for ticker in tickers:
        score = 0
        sources = []
        details = {}

        # Insider/Congress data
        moves = (await db.execute(
            select(StockWhaleMove)
            .where(StockWhaleMove.ticker == ticker,
                   StockWhaleMove.filing_date >= cutoff_30d)
        )).scalars().all()

        if moves:
            insider_buys = [m for m in moves if m.source == "insider" and any(k in (m.action or "").lower() for k in buy_keywords)]
            insider_sells = [m for m in moves if m.source == "insider" and any(k in (m.action or "").lower() for k in sell_keywords)]
            congress_buys = [m for m in moves if m.source == "congress" and any(k in (m.action or "").lower() for k in buy_keywords)]

            if insider_buys:
                score += min(len(insider_buys) * 5, 25)
                sources.append("insider_buys")
            if insider_sells:
                score -= min(len(insider_sells) * 3, 15)
            if congress_buys:
                score += min(len(congress_buys) * 8, 20)
                sources.append("congress")

            details["insiders"] = {
                "buys": len(insider_buys),
                "sells": len(insider_sells),
                "congress": len(congress_buys),
            }

        # Options flow sentiment
        uw_key = os.getenv("UNUSUAL_WHALES_KEY", "")
        if uw_key:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get("https://api.unusualwhales.com/api/option-trades/flow-alerts",
                                            headers={"Authorization": f"Bearer {uw_key}"}, timeout=8)
                    if resp.status_code == 200:
                        flow = [a for a in resp.json().get("data", []) if a.get("ticker") == ticker]
                        if flow:
                            ask_total = sum(float(a.get("total_ask_side_prem", 0) or 0) for a in flow)
                            bid_total = sum(float(a.get("total_bid_side_prem", 0) or 0) for a in flow)
                            total_prem = sum(float(a.get("total_premium", 0) or 0) for a in flow)
                            if ask_total > bid_total * 1.3:
                                score += 15
                                sources.append("options_bullish")
                            elif bid_total > ask_total * 1.3:
                                score -= 10
                                sources.append("options_bearish")
                            details["options"] = {"premium": total_prem, "bullish_prem": ask_total, "bearish_prem": bid_total}
            except:
                pass

        # Social sentiment
        from ..workers.sentiment_tracker import SocialMention
        cutoff_24h = datetime.utcnow() - timedelta(hours=24)
        sent = (await db.execute(
            select(func.count(), func.avg(SocialMention.sentiment_score))
            .where(SocialMention.token_symbol == ticker, SocialMention.detected_at >= cutoff_24h)
        )).first()
        if sent and sent[0] and int(sent[0]) >= 3:
            avg_sent = float(sent[1] or 0)
            if avg_sent > 10:
                score += 10
                sources.append("social_bullish")
            elif avg_sent < -10:
                score -= 5
            details["social"] = {"mentions": int(sent[0]), "avg_score": round(avg_sent, 1)}

        score = max(0, min(100, score))
        signal = "BUY" if score >= 40 and len(sources) >= 2 else "SELL" if score <= 10 else "HOLD"

        if sources:
            results.append({
                "ticker": ticker,
                "signal": signal,
                "agiotage_score": score,
                "sources": sources,
                "source_count": len(sources),
                "details": details,
            })

    results.sort(key=lambda x: x["agiotage_score"], reverse=True)
    return {"count": len(results), "stocks": results, "generated_at": datetime.utcnow().isoformat()}


@router.get("/scan/market")
async def scan_market(db: AsyncSession = Depends(get_db)):
    """Scan all tracked tokens and return the top signals."""
    tokens = ["BTC", "ETH", "SOL", "XRP", "LINK", "AVAX", "DOT", "ADA", "SUI", "DOGE",
              "PEPE", "BONK", "WIF", "SHIB", "FLOKI", "ARB", "OP", "NEAR", "INJ", "TAO"]

    results = []
    for token in tokens:
        try:
            alpha = await get_alpha(token, db)
            if alpha.get("sources"):
                results.append({
                    "token": alpha["token"],
                    "signal": alpha["signal"],
                    "confidence": alpha["confidence"],
                    "source_count": alpha["source_count"],
                    "whale_flow": alpha["whale_flow"],
                    "galaxy_score": alpha.get("galaxy_score"),
                    "summary": alpha["summary"],
                })
        except Exception:
            pass

    results.sort(key=lambda x: x["confidence"], reverse=True)
    return {"count": len(results), "market": results, "generated_at": datetime.utcnow().isoformat()}

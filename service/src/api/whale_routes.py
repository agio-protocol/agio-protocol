# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Whale Tracker + Stocks API — crypto whale alerts, 13F filings, insider trades, Congress trades."""
from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db

router = APIRouter(prefix="/v1/whales", tags=["whales"])


async def _require_auth(authorization: str):
    if not authorization or not authorization.startswith("Bearer ses_"):
        raise HTTPException(401, "Sign in to access whale tracker")
    token = authorization.replace("Bearer ", "")
    from ..core.redis import redis_client
    if not await redis_client.get(f"session:{token}"):
        raise HTTPException(401, "Session expired")


@router.get("/crypto/feed")
async def crypto_whale_feed(
    symbol: str = Query(None),
    tx_type: str = Query(None),
    min_usd: float = Query(0),
    limit: int = Query(50, ge=1, le=200),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Recent large crypto transfers."""
    await _require_auth(authorization)
    from ..workers.whale_tracker import WhaleTransaction

    query = select(WhaleTransaction)
    if symbol:
        query = query.where(WhaleTransaction.symbol == symbol.upper())
    if tx_type:
        query = query.where(WhaleTransaction.tx_type == tx_type)
    if min_usd > 0:
        query = query.where(WhaleTransaction.amount_usd >= min_usd)
    query = query.order_by(WhaleTransaction.trade_time.desc()).limit(limit)
    txns = (await db.execute(query)).scalars().all()

    return {
        "count": len(txns),
        "transactions": [
            {
                "tx_hash": t.tx_hash,
                "blockchain": t.blockchain,
                "symbol": t.symbol,
                "amount": float(t.amount),
                "amount_usd": float(t.amount_usd),
                "from": t.from_owner,
                "to": t.to_owner,
                "type": t.tx_type,
                "time": t.trade_time.isoformat(),
            }
            for t in txns
        ],
    }


@router.get("/crypto/signals")
async def crypto_signals(
    direction: str = Query(None),
    symbol: str = Query(None),
    limit: int = Query(20, ge=1, le=100),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Crypto buy/sell pressure signals from whale exchange flows."""
    await _require_auth(authorization)
    from ..workers.whale_tracker import CryptoSignal, STABLECOINS
    import json as _json

    stables_upper = {s.upper() for s in STABLECOINS}
    query = select(CryptoSignal).where(~CryptoSignal.symbol.in_(stables_upper))
    if direction:
        query = query.where(CryptoSignal.direction == direction.upper())
    if symbol:
        query = query.where(CryptoSignal.symbol == symbol.upper())
    query = query.order_by(CryptoSignal.detected_at.desc()).limit(limit)
    signals = (await db.execute(query)).scalars().all()

    # Deduplicate — keep only ONE signal per symbol (the dominant direction)
    symbol_signals = {}
    for s in signals:
        if s.symbol not in symbol_signals:
            symbol_signals[s.symbol] = s
        else:
            # Keep the one with more USD volume
            existing = symbol_signals[s.symbol]
            if float(s.total_usd or 0) > float(existing.total_usd or 0):
                symbol_signals[s.symbol] = s
    signals = list(symbol_signals.values())

    return {
        "count": len(signals),
        "signals": [
            {
                "symbol": s.symbol,
                "signal_type": s.signal_type,
                "direction": s.direction,
                "strength": s.strength,
                "tx_count": s.tx_count,
                "total_usd": float(s.total_usd or 0),
                "exchanges": s.exchanges_involved.split(",") if s.exchanges_involved else [],
                "description": s.description,
                "transactions": _json.loads(s.transactions_json) if s.transactions_json else [],
                "detected_at": s.detected_at.isoformat(),
            }
            for s in signals
        ],
    }


@router.get("/crypto/flow-analysis")
async def crypto_flow_analysis(
    hours: int = Query(6, ge=1, le=48),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Analyzed exchange flows — net direction per coin with actionable signals."""
    await _require_auth(authorization)
    from ..workers.whale_tracker import WhaleTransaction
    from datetime import datetime, timedelta
    from collections import defaultdict

    cutoff = datetime.utcnow() - timedelta(hours=hours)

    from ..workers.whale_tracker import STABLECOINS
    stables_upper = {s.upper() for s in STABLECOINS}
    txns = (await db.execute(
        select(WhaleTransaction)
        .where(WhaleTransaction.trade_time >= cutoff,
               WhaleTransaction.tx_type.in_(["exchange_deposit", "exchange_withdrawal"]),
               ~WhaleTransaction.symbol.in_(stables_upper))
    )).scalars().all()

    # Aggregate by coin
    coin_flows = defaultdict(lambda: {
        "deposits": 0, "deposit_usd": 0, "deposit_count": 0,
        "withdrawals": 0, "withdrawal_usd": 0, "withdrawal_count": 0,
        "exchanges": defaultdict(lambda: {"in": 0, "out": 0}),
        "largest_single": 0, "largest_detail": "",
    })

    for t in txns:
        cf = coin_flows[t.symbol]
        usd = float(t.amount_usd)
        if t.tx_type == "exchange_deposit":
            cf["deposits"] += float(t.amount)
            cf["deposit_usd"] += usd
            cf["deposit_count"] += 1
            exchange = t.to_owner or "unknown"
            cf["exchanges"][exchange]["in"] += usd
        else:
            cf["withdrawals"] += float(t.amount)
            cf["withdrawal_usd"] += usd
            cf["withdrawal_count"] += 1
            exchange = t.from_owner or "unknown"
            cf["exchanges"][exchange]["out"] += usd

        if usd > cf["largest_single"]:
            cf["largest_single"] = usd
            cf["largest_detail"] = f"${usd:,.0f} {t.symbol} {'to' if t.tx_type=='exchange_deposit' else 'from'} {t.to_owner or t.from_owner or 'unknown'}"

    # Build analysis
    results = []
    for symbol, data in sorted(coin_flows.items(), key=lambda x: abs(x[1]["deposit_usd"] - x[1]["withdrawal_usd"]), reverse=True):
        net_flow = data["deposit_usd"] - data["withdrawal_usd"]
        total_volume = data["deposit_usd"] + data["withdrawal_usd"]

        if total_volume < 50000:
            continue

        # Direction and strength
        if net_flow > 0:
            direction = "SELL_PRESSURE"
            pct = (net_flow / total_volume * 100) if total_volume else 0
        else:
            direction = "ACCUMULATION"
            pct = (abs(net_flow) / total_volume * 100) if total_volume else 0

        if pct > 70:
            strength = "STRONG"
        elif pct > 40:
            strength = "MODERATE"
        else:
            strength = "MIXED"

        # Build exchange breakdown
        exchange_list = []
        for exch, flows in data["exchanges"].items():
            if flows["in"] > 0 or flows["out"] > 0:
                exchange_list.append({
                    "exchange": exch.title(),
                    "inflow_usd": round(flows["in"], 2),
                    "outflow_usd": round(flows["out"], 2),
                    "net": round(flows["in"] - flows["out"], 2),
                })
        exchange_list.sort(key=lambda x: abs(x["net"]), reverse=True)

        # Action summary
        if direction == "SELL_PRESSURE" and strength == "STRONG":
            action = f"CAUTION: ${abs(net_flow):,.0f} net flowing INTO exchanges. Whales may be preparing to sell."
        elif direction == "SELL_PRESSURE":
            action = f"Mild sell pressure: ${abs(net_flow):,.0f} net inflow to exchanges."
        elif direction == "ACCUMULATION" and strength == "STRONG":
            action = f"BULLISH: ${abs(net_flow):,.0f} net flowing OUT of exchanges. Whales are accumulating."
        else:
            action = f"Mild accumulation: ${abs(net_flow):,.0f} net outflow from exchanges."

        results.append({
            "symbol": symbol,
            "direction": direction,
            "strength": strength,
            "net_flow_usd": round(net_flow, 2),
            "deposit_usd": round(data["deposit_usd"], 2),
            "withdrawal_usd": round(data["withdrawal_usd"], 2),
            "deposit_count": data["deposit_count"],
            "withdrawal_count": data["withdrawal_count"],
            "total_volume": round(total_volume, 2),
            "flow_ratio": round(pct, 1),
            "largest_single": data["largest_detail"],
            "exchanges": exchange_list[:5],
            "action": action,
        })

    return {"hours": hours, "coins": results}


@router.get("/crypto/stats")
async def crypto_whale_stats(db: AsyncSession = Depends(get_db)):
    """Public crypto whale stats."""
    from ..workers.whale_tracker import WhaleTransaction, CryptoSignal
    total = (await db.execute(select(func.count()).select_from(WhaleTransaction))).scalar() or 0
    deposits = (await db.execute(
        select(func.count()).select_from(WhaleTransaction).where(WhaleTransaction.tx_type == "exchange_deposit")
    )).scalar() or 0
    withdrawals = (await db.execute(
        select(func.count()).select_from(WhaleTransaction).where(WhaleTransaction.tx_type == "exchange_withdrawal")
    )).scalar() or 0
    signals = (await db.execute(select(func.count()).select_from(CryptoSignal))).scalar() or 0
    buy_signals = (await db.execute(
        select(func.count()).select_from(CryptoSignal).where(CryptoSignal.direction == "BUY")
    )).scalar() or 0
    sell_signals = (await db.execute(
        select(func.count()).select_from(CryptoSignal).where(CryptoSignal.direction == "SELL")
    )).scalar() or 0
    return {"total_transactions": total, "exchange_deposits": deposits, "exchange_withdrawals": withdrawals,
            "signals": signals, "buy_signals": buy_signals, "sell_signals": sell_signals}


@router.get("/stocks/feed")
async def stocks_whale_feed(
    source: str = Query(None),
    ticker: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Recent stock whale moves — 13F filings, insider trades, Congress trades."""
    await _require_auth(authorization)
    from ..workers.stocks_tracker import StockWhaleMove

    query = select(StockWhaleMove)
    if source:
        query = query.where(StockWhaleMove.source == source)
    if ticker:
        query = query.where(StockWhaleMove.ticker == ticker.upper())
    query = query.order_by(StockWhaleMove.filing_date.desc()).limit(limit)
    moves = (await db.execute(query)).scalars().all()

    return {
        "count": len(moves),
        "moves": [
            {
                "source": m.source,
                "filer": m.filer_name,
                "ticker": m.ticker,
                "company": m.company_name,
                "action": m.action,
                "shares": float(m.shares) if m.shares else None,
                "price": round(float(m.value_usd) / float(m.shares), 2) if m.value_usd and m.shares and float(m.shares) > 0 else None,
                "value_usd": float(m.value_usd) if m.value_usd else None,
                "filing_date": m.filing_date.isoformat(),
                "filing_url": m.filing_url,
            }
            for m in moves
        ],
    }


@router.get("/stocks/signals")
async def stock_signals(
    min_strength: str = Query(None),
    ticker: str = Query(None),
    limit: int = Query(20, ge=1, le=100),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Stock convergence signals — insiders + Congress + hedge funds buying the same stock."""
    await _require_auth(authorization)
    from ..workers.stocks_tracker import StockSignal
    import json as _json

    query = select(StockSignal)
    if min_strength:
        strength_order = {"VERY_STRONG": 4, "STRONG": 3, "MEDIUM": 2}
        min_val = strength_order.get(min_strength.upper(), 0)
        allowed = [s for s, v in strength_order.items() if v >= min_val]
        query = query.where(StockSignal.strength.in_(allowed))
    if ticker:
        query = query.where(StockSignal.ticker == ticker.upper())

    query = query.order_by(StockSignal.detected_at.desc()).limit(limit)
    signals = (await db.execute(query)).scalars().all()

    return {
        "count": len(signals),
        "signals": [
            {
                "ticker": s.ticker,
                "signal_type": s.signal_type,
                "strength": s.strength,
                "filer_count": s.filer_count,
                "sources": s.sources.split(",") if s.sources else [],
                "total_value": float(s.total_value or 0),
                "description": s.description,
                "filers": _json.loads(s.filers_json) if s.filers_json else [],
                "detected_at": s.detected_at.isoformat(),
            }
            for s in signals
        ],
    }


@router.get("/stocks/analysis")
async def stocks_analysis(
    days: int = Query(30, ge=1, le=90),
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Compiled stock analysis — which tickers have the most insider/Congress/13F activity."""
    await _require_auth(authorization)
    from ..workers.stocks_tracker import StockWhaleMove
    from datetime import datetime, timedelta
    from collections import defaultdict

    cutoff = datetime.utcnow() - timedelta(days=days)

    moves = (await db.execute(
        select(StockWhaleMove)
        .where(StockWhaleMove.filing_date >= cutoff,
               StockWhaleMove.ticker.isnot(None),
               StockWhaleMove.ticker != "")
    )).scalars().all()

    # Aggregate by ticker
    ticker_data = defaultdict(lambda: {
        "insider_buys": [], "insider_sells": [],
        "congress_buys": [], "congress_sells": [],
        "filings_13f": [],
        "total_value": 0, "filer_names": set(),
    })

    buy_keywords = {"purchase", "buy", "acquired", "exercise"}
    sell_keywords = {"sale", "sell", "sold", "disposition"}

    for m in moves:
        tk = m.ticker.upper()
        td = ticker_data[tk]
        td["filer_names"].add(m.filer_name)
        if m.value_usd:
            td["total_value"] += float(m.value_usd)

        action_lower = (m.action or "").lower()
        is_buy = any(k in action_lower for k in buy_keywords)
        is_sell = any(k in action_lower for k in sell_keywords)

        entry = {
            "filer": m.filer_name,
            "date": m.filing_date.strftime("%Y-%m-%d"),
            "shares": float(m.shares) if m.shares else None,
            "value": float(m.value_usd) if m.value_usd else None,
            "price": round(float(m.value_usd) / float(m.shares), 2) if m.value_usd and m.shares and float(m.shares) > 0 else None,
            "action": m.action,
        }

        if m.source == "insider":
            if is_buy:
                td["insider_buys"].append(entry)
            elif is_sell:
                td["insider_sells"].append(entry)
            else:
                td["insider_buys"].append(entry)
        elif m.source == "congress":
            if is_sell:
                td["congress_sells"].append(entry)
            else:
                td["congress_buys"].append(entry)
        elif m.source == "13f":
            td["filings_13f"].append(entry)

    # Build analysis per ticker
    results = []
    for ticker, data in ticker_data.items():
        insider_buy_count = len(data["insider_buys"])
        insider_sell_count = len(data["insider_sells"])
        congress_buy_count = len(data["congress_buys"])
        congress_sell_count = len(data["congress_sells"])
        filing_count = len(data["filings_13f"])
        total_activity = insider_buy_count + insider_sell_count + congress_buy_count + congress_sell_count + filing_count

        if total_activity < 1:
            continue

        # Direction analysis
        buy_signals = insider_buy_count + congress_buy_count
        sell_signals = insider_sell_count + congress_sell_count

        if buy_signals > sell_signals + 1:
            direction = "BULLISH"
            dir_color = "green"
        elif sell_signals > buy_signals + 1:
            direction = "BEARISH"
            dir_color = "red"
        else:
            direction = "MIXED"
            dir_color = "neutral"

        # Strength
        source_count = sum([
            1 if insider_buy_count + insider_sell_count > 0 else 0,
            1 if congress_buy_count + congress_sell_count > 0 else 0,
            1 if filing_count > 0 else 0,
        ])
        if source_count >= 3 and total_activity >= 5:
            strength = "STRONG"
        elif source_count >= 2 or total_activity >= 3:
            strength = "MODERATE"
        else:
            strength = "LIGHT"

        # Build action summary
        parts = []
        if insider_buy_count:
            names = list(set(e["filer"] for e in data["insider_buys"]))[:3]
            parts.append(f"{insider_buy_count} insider buy{'s' if insider_buy_count>1 else ''} ({', '.join(n[:20] for n in names)})")
        if insider_sell_count:
            names = list(set(e["filer"] for e in data["insider_sells"]))[:3]
            parts.append(f"{insider_sell_count} insider sell{'s' if insider_sell_count>1 else ''} ({', '.join(n[:20] for n in names)})")
        if congress_buy_count:
            names = list(set(e["filer"] for e in data["congress_buys"]))[:3]
            parts.append(f"{congress_buy_count} Congress buy{'s' if congress_buy_count>1 else ''} ({', '.join(n[:20] for n in names)})")
        if congress_sell_count:
            names = list(set(e["filer"] for e in data["congress_sells"]))[:3]
            parts.append(f"{congress_sell_count} Congress sell{'s' if congress_sell_count>1 else ''}")
        if filing_count:
            parts.append(f"{filing_count} 13F filing{'s' if filing_count>1 else ''}")

        results.append({
            "ticker": ticker,
            "direction": direction,
            "strength": strength,
            "total_activity": total_activity,
            "insider_buys": insider_buy_count,
            "insider_sells": insider_sell_count,
            "congress_buys": congress_buy_count,
            "congress_sells": congress_sell_count,
            "filings_13f": filing_count,
            "total_value": round(data["total_value"], 2),
            "unique_filers": len(data["filer_names"]),
            "summary": " · ".join(parts),
            "recent_moves": sorted(
                data["insider_buys"][:3] + data["congress_buys"][:3] + data["insider_sells"][:2],
                key=lambda x: x["date"], reverse=True
            )[:5],
        })

    results.sort(key=lambda x: x["total_activity"], reverse=True)
    return {"days": days, "tickers": results}


@router.get("/stocks/stats")
async def stocks_whale_stats(db: AsyncSession = Depends(get_db)):
    """Public stock whale stats."""
    from ..workers.stocks_tracker import StockWhaleMove, StockSignal
    total = (await db.execute(select(func.count()).select_from(StockWhaleMove))).scalar() or 0
    congress = (await db.execute(
        select(func.count()).select_from(StockWhaleMove).where(StockWhaleMove.source == "congress")
    )).scalar() or 0
    insider = (await db.execute(
        select(func.count()).select_from(StockWhaleMove).where(StockWhaleMove.source == "insider")
    )).scalar() or 0
    filings_13f = (await db.execute(
        select(func.count()).select_from(StockWhaleMove).where(StockWhaleMove.source == "13f")
    )).scalar() or 0
    signals = (await db.execute(select(func.count()).select_from(StockSignal))).scalar() or 0
    return {"total_moves": total, "congress_trades": congress, "insider_trades": insider, "filings_13f": filings_13f, "signals": signals}

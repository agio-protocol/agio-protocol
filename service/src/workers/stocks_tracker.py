# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Stock Whale Tracker — 13F filings, insider trades, Congress trades via SEC EDGAR."""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, func, String, Text, Integer, BigInteger, Numeric, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.base import Base

_log = logging.getLogger("stocks-tracker")

SEC_EDGAR_BASE = "https://efts.sec.gov/LATEST/search-index"
SEC_FILINGS = "https://efts.sec.gov/LATEST/search-index?q=%2213F%22&dateRange=custom&startdt={start}&enddt={end}&forms=13F-HR"
SEC_INSIDER = "https://efts.sec.gov/LATEST/search-index?forms=4&dateRange=custom&startdt={start}&enddt={end}"
CONGRESS_API = "https://bythebay.cool/api/v1/trades"

POLL_INTERVAL = 3600
USER_AGENT = "AgiotageBot/1.0 (j2422144@gmail.com)"


class StockWhaleMove(Base):
    __tablename__ = "stock_whale_moves"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    filer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(20), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    shares: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    value_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    filing_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    filing_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    unique_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_stock_source", "source"),
        Index("idx_stock_ticker", "ticker"),
        Index("idx_stock_date", "filing_date"),
    )


async def _fetch_13f_filings():
    """Fetch recent 13F filings from SEC EDGAR."""
    end = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                "https://efts.sec.gov/LATEST/search-index",
                params={"q": "13F", "forms": "13F-HR", "dateRange": "custom",
                        "startdt": start, "enddt": end},
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            if resp.status_code != 200:
                # Try alternative EDGAR full-text search
                resp = await client.get(
                    "https://efts.sec.gov/LATEST/search-index",
                    params={"q": '"13F-HR"', "startdt": start, "enddt": end},
                    headers={"User-Agent": USER_AGENT},
                    timeout=30,
                )
            if resp.status_code != 200:
                _log.debug(f"SEC EDGAR 13F returned {resp.status_code}")
                return
            data = resp.json()
            filings = data.get("hits", {}).get("hits", [])
            _log.info(f"Found {len(filings)} recent 13F filings")
        except Exception as e:
            _log.debug(f"SEC EDGAR fetch failed: {e}")
            return

    async with async_session() as db:
        for f in filings[:50]:
            source = f.get("_source", {})
            filer = source.get("display_names", ["Unknown"])[0] if source.get("display_names") else "Unknown"
            date_str = source.get("file_date", "")
            filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={source.get('entity_id','')}&type=13F"

            key = f"13f:{filer}:{date_str}"
            existing = (await db.execute(
                select(StockWhaleMove).where(StockWhaleMove.unique_key == key)
            )).scalar_one_or_none()
            if existing:
                continue

            try:
                filing_date = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.utcnow()
            except ValueError:
                filing_date = datetime.utcnow()

            move = StockWhaleMove(
                source="13f",
                filer_name=filer[:200],
                action="13F filing",
                filing_date=filing_date,
                filing_url=filing_url,
                unique_key=key,
            )
            db.add(move)

        await db.commit()


_cik_to_ticker = {}
_cik_cache_loaded = False


async def _load_cik_tickers(client: httpx.AsyncClient):
    """Load SEC CIK-to-ticker mapping."""
    global _cik_to_ticker, _cik_cache_loaded
    if _cik_cache_loaded:
        return
    try:
        resp = await client.get("https://www.sec.gov/files/company_tickers.json",
                                headers={"User-Agent": USER_AGENT}, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            for v in data.values():
                cik = str(v.get("cik_str", "")).zfill(10)
                _cik_to_ticker[cik] = v.get("ticker", "")
            _cik_cache_loaded = True
            _log.info(f"Loaded {len(_cik_to_ticker)} CIK-to-ticker mappings")
    except Exception as e:
        _log.debug(f"CIK ticker load failed: {e}")


async def _fetch_insider_trades():
    """Fetch recent SEC Form 4 insider trades with ticker and action details."""
    end = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

    async with httpx.AsyncClient() as client:
        await _load_cik_tickers(client)

        try:
            resp = await client.get(
                "https://efts.sec.gov/LATEST/search-index",
                params={"forms": "4", "dateRange": "custom",
                        "startdt": start, "enddt": end},
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            if resp.status_code != 200:
                _log.debug(f"SEC Form 4 returned {resp.status_code}")
                return
            data = resp.json()
            filings = data.get("hits", {}).get("hits", [])
            _log.info(f"Found {len(filings)} recent insider trades (Form 4)")
        except Exception as e:
            _log.debug(f"SEC Form 4 fetch failed: {e}")
            return

    async with async_session() as db, httpx.AsyncClient() as client:
        for f in filings[:100]:
            source = f.get("_source", {})
            display_names = source.get("display_names", [])
            ciks = source.get("ciks", [])
            date_str = source.get("file_date", "")

            # First display_name = insider, second = company
            filer = display_names[0] if display_names else "Unknown"
            issuer = display_names[1] if len(display_names) > 1 else ""

            # Clean up filer name — remove CIK from display
            import re
            filer_clean = re.sub(r'\s*\(CIK\s+\d+\)', '', filer).strip()
            issuer_clean = re.sub(r'\s*\(CIK\s+\d+\)', '', issuer).strip()

            # Look up ticker from company CIK (second CIK)
            ticker = ""
            if len(ciks) > 1:
                company_cik = ciks[1].zfill(10) if isinstance(ciks[1], str) else str(ciks[1]).zfill(10)
                ticker = _cik_to_ticker.get(company_cik, "")

            # Determine buy/sell from filing XML (simplified — check period)
            action = "Insider Purchase" if "purchase" in issuer_clean.lower() else "Insider Trade (Form 4)"

            key = f"form4:{filer_clean}:{date_str}:{ticker or issuer_clean}"
            existing = (await db.execute(
                select(StockWhaleMove).where(StockWhaleMove.unique_key == key)
            )).scalar_one_or_none()
            if existing:
                continue

            try:
                filing_date = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.utcnow()
            except ValueError:
                filing_date = datetime.utcnow()

            # Build SEC filing URL
            adsh = source.get("adsh", "")
            filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ciks[0] if ciks else ''}&type=4" if ciks else None

            # Fetch Form 4 XML for transaction details
            shares = None
            price_per_share = None
            tx_code = ""
            if adsh and len(ciks) > 1:
                try:
                    company_cik_raw = ciks[1] if isinstance(ciks[1], str) else str(ciks[1])
                    company_cik_clean = company_cik_raw.lstrip('0') or '0'
                    adsh_dashes = adsh
                    adsh_clean = adsh.replace('-', '')
                    # Try the filing index to find the actual XML filename
                    index_url = f"https://www.sec.gov/Archives/edgar/data/{company_cik_clean}/{adsh_clean}"
                    xml_url = None
                    # Try common filenames
                    filing_id = f.get("_id", "")
                    for fname in [filing_id.split(":")[-1] if ":" in filing_id else "",
                                  "primary_doc.xml", "form4.xml", "ownership.xml",
                                  f"wk-form4_{adsh_clean}.xml"]:
                        if not fname:
                            continue
                        test_url = f"{index_url}/{fname}"
                        test_resp = await client.head(test_url, headers={"User-Agent": USER_AGENT}, timeout=5)
                        if test_resp.status_code == 200:
                            xml_url = test_url
                            break
                        await asyncio.sleep(0.1)

                    if not xml_url:
                        xml_url = f"{index_url}/form4.xml"

                    xml_resp = await client.get(xml_url, headers={"User-Agent": USER_AGENT}, timeout=10)
                    if xml_resp.status_code == 200:
                        import xml.etree.ElementTree as ET
                        root = ET.fromstring(xml_resp.text)
                        # Strip namespace prefixes for easier parsing
                        ns = ""
                        for elem in root.iter():
                            if '}' in elem.tag:
                                ns = elem.tag.split('}')[0] + '}'
                                break

                        # Parse non-derivative transactions (most insider buys/sells)
                        for tx in root.iter(f"{ns}nonDerivativeTransaction"):
                            code_elem = tx.find(f".//{ns}transactionCode")
                            if code_elem is not None and code_elem.text:
                                tx_code = code_elem.text.strip()

                            shares_elem = tx.find(f".//{ns}transactionAmounts/{ns}transactionShares/{ns}value")
                            if shares_elem is not None and shares_elem.text:
                                try:
                                    shares = float(shares_elem.text.strip())
                                except ValueError:
                                    pass

                            price_elem = tx.find(f".//{ns}transactionAmounts/{ns}transactionPricePerShare/{ns}value")
                            if price_elem is not None and price_elem.text:
                                try:
                                    price_per_share = float(price_elem.text.strip())
                                except ValueError:
                                    pass

                            if tx_code:
                                break

                        # Fallback: try without namespace
                        if not tx_code:
                            for tx in root.iter("nonDerivativeTransaction"):
                                code_elem = tx.find(".//transactionCode")
                                if code_elem is not None and code_elem.text:
                                    tx_code = code_elem.text.strip()
                                s_elem = tx.find(".//transactionShares/value")
                                if s_elem is not None and s_elem.text:
                                    try:
                                        shares = float(s_elem.text.strip())
                                    except ValueError:
                                        pass
                                p_elem = tx.find(".//transactionPricePerShare/value")
                                if p_elem is not None and p_elem.text:
                                    try:
                                        price_per_share = float(p_elem.text.strip())
                                    except ValueError:
                                        pass
                                if tx_code:
                                    break

                    await asyncio.sleep(0.3)
                except Exception as e:
                    _log.debug(f"Form 4 XML parse error for {adsh}: {e}")

            # Map transaction code to action
            code_map = {"P": "Purchase", "S": "Sale", "A": "Award/Grant", "M": "Option Exercise",
                        "F": "Tax Payment", "G": "Gift", "C": "Conversion"}
            if tx_code:
                action = f"Insider {code_map.get(tx_code, tx_code)}"

            value = None
            if shares and price_per_share:
                value = shares * price_per_share

            move = StockWhaleMove(
                source="insider",
                filer_name=filer_clean[:200],
                ticker=ticker[:20] if ticker else None,
                company_name=issuer_clean[:200],
                action=action,
                shares=Decimal(str(round(shares, 2))) if shares else None,
                value_usd=Decimal(str(round(value, 2))) if value else None,
                filing_date=filing_date,
                filing_url=filing_url,
                unique_key=key,
            )
            db.add(move)

        await db.commit()


async def _fetch_congress_trades():
    """Fetch recent Congress trading disclosures."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
                timeout=30,
            )
            if resp.status_code != 200:
                _log.debug(f"Congress trades returned {resp.status_code}")
                return
            trades = resp.json()
        except Exception as e:
            _log.debug(f"Congress trades fetch failed: {e}")
            return

    cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

    async with async_session() as db:
        recent = [t for t in trades if t.get("transaction_date", "") >= cutoff]
        _log.info(f"Found {len(recent)} recent Congress trades")

        for t in recent[:200]:
            rep = t.get("representative", "Unknown")
            ticker = t.get("ticker", "")
            tx_type = t.get("type", "")
            amount = t.get("amount", "")
            date_str = t.get("transaction_date", "")
            desc = t.get("asset_description", "")

            key = f"congress:{rep}:{ticker}:{date_str}:{tx_type}"
            existing = (await db.execute(
                select(StockWhaleMove).where(StockWhaleMove.unique_key == key)
            )).scalar_one_or_none()
            if existing:
                continue

            try:
                filing_date = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.utcnow()
            except ValueError:
                filing_date = datetime.utcnow()

            # Parse amount range like "$1,001 - $15,000"
            value = None
            if amount and "-" in amount:
                try:
                    high = amount.split("-")[1].strip().replace("$", "").replace(",", "")
                    value = float(high)
                except (ValueError, IndexError):
                    pass

            move = StockWhaleMove(
                source="congress",
                filer_name=rep[:200],
                ticker=ticker[:20] if ticker else None,
                company_name=desc[:200] if desc else None,
                action=tx_type,
                value_usd=Decimal(str(value)) if value else None,
                filing_date=filing_date,
                unique_key=key,
            )
            db.add(move)

        await db.commit()


class StockSignal(Base):
    __tablename__ = "stock_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    strength: Mapped[str] = mapped_column(String(20), default="MEDIUM")
    filer_count: Mapped[int] = mapped_column(Integer, default=0)
    sources: Mapped[str | None] = mapped_column(Text, nullable=True)
    filers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_value: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_stock_signal_time", "detected_at"),
        Index("idx_stock_signal_ticker", "ticker"),
    )


async def _detect_stock_signals():
    """Detect convergence signals across insiders, Congress, and 13F filings."""
    import json as _json

    async with async_session() as db:
        cutoff_30d = datetime.utcnow() - timedelta(days=30)
        cutoff_90d = datetime.utcnow() - timedelta(days=90)

        # Get all recent moves with tickers
        recent_moves = (await db.execute(
            select(StockWhaleMove)
            .where(StockWhaleMove.ticker.isnot(None),
                   StockWhaleMove.ticker != "",
                   StockWhaleMove.filing_date >= cutoff_90d)
        )).scalars().all()

        if not recent_moves:
            return

        # Group by ticker
        from collections import defaultdict
        ticker_groups = defaultdict(list)
        for m in recent_moves:
            if m.ticker:
                ticker_groups[m.ticker.upper()].append(m)

        new_signals = 0
        for ticker, moves in ticker_groups.items():
            # Filter to buys/purchases only
            buy_keywords = {"purchase", "buy", "acquired", "13f filing", "insider trade"}
            buys = [m for m in moves if any(k in (m.action or "").lower() for k in buy_keywords) or m.source == "13f"]

            if len(buys) < 2:
                continue

            # Check for existing signal on this ticker in last 7 days
            existing = (await db.execute(
                select(StockSignal)
                .where(StockSignal.ticker == ticker,
                       StockSignal.detected_at >= datetime.utcnow() - timedelta(days=7))
            )).scalar_one_or_none()
            if existing:
                continue

            # Count unique sources and filers
            sources_set = set(m.source for m in buys)
            unique_filers = set(m.filer_name for m in buys)
            filer_count = len(unique_filers)

            # Insider cluster — 3+ insiders at same company buying
            insider_buys = [m for m in buys if m.source == "insider"]
            congress_buys = [m for m in buys if m.source == "congress"]
            filing_buys = [m for m in buys if m.source == "13f"]

            # Determine signal type and strength
            signal_type = None
            strength = "MEDIUM"

            if len(sources_set) >= 3:
                signal_type = "cross_source"
                strength = "VERY_STRONG"
            elif len(sources_set) >= 2 and filer_count >= 3:
                signal_type = "cross_source"
                strength = "STRONG"
            elif len(insider_buys) >= 3:
                signal_type = "insider_cluster"
                strength = "STRONG" if len(insider_buys) >= 5 else "MEDIUM"
            elif len(congress_buys) >= 2:
                signal_type = "congress_cluster"
                strength = "STRONG" if len(congress_buys) >= 3 else "MEDIUM"
            elif len(filing_buys) >= 3:
                signal_type = "13f_convergence"
                strength = "STRONG" if len(filing_buys) >= 5 else "MEDIUM"

            if not signal_type:
                continue

            total_val = sum(float(m.value_usd or 0) for m in buys)
            filers_info = [{"name": m.filer_name, "source": m.source,
                           "action": m.action, "date": m.filing_date.isoformat(),
                           "value": float(m.value_usd or 0)} for m in buys]

            # Build description
            parts = []
            if insider_buys:
                parts.append(f"{len(set(m.filer_name for m in insider_buys))} insider(s)")
            if congress_buys:
                parts.append(f"{len(set(m.filer_name for m in congress_buys))} Congress member(s)")
            if filing_buys:
                parts.append(f"{len(set(m.filer_name for m in filing_buys))} hedge fund(s)")
            desc = f"${ticker}: {', '.join(parts)} buying in the last {90 if signal_type == '13f_convergence' else 30} days"

            signal = StockSignal(
                ticker=ticker,
                signal_type=signal_type,
                strength=strength,
                filer_count=filer_count,
                sources=",".join(sources_set),
                filers_json=_json.dumps(filers_info),
                total_value=Decimal(str(total_val)) if total_val else None,
                description=desc,
            )
            db.add(signal)
            new_signals += 1

            _log.warning(f"STOCK SIGNAL [{strength}] {signal_type}: ${ticker} — {desc}")

            # Notify
            from ..models.platform import Notification
            notif = Notification(
                agent_id="0xb18a31796ea51c52c203c96aab0b1bc551c4e051",
                type="stock_signal",
                title=f"Stock Signal [{strength}]: ${ticker}",
                body=desc,
                link="/trading.html",
            )
            db.add(notif)

        if new_signals:
            await db.commit()
            _log.info(f"Detected {new_signals} new stock signals")


async def run():
    _log.info("Stocks tracker starting — monitoring 13F filings, insider trades, Congress trades")
    await asyncio.sleep(30)
    while True:
        try:
            await _fetch_congress_trades()
            await asyncio.sleep(10)
            await _fetch_13f_filings()
            await asyncio.sleep(10)
            await _fetch_insider_trades()
            await asyncio.sleep(10)
            await _detect_stock_signals()
        except Exception as e:
            _log.error(f"Stocks tracker error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

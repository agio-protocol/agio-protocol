# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Telegram Alert Bot — pushes high-value signals to your phone."""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session

_log = logging.getLogger("telegram-alerts")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

POLL_INTERVAL = 60
_sent_ids = set()
_is_primary = False


async def _claim_primary():
    """Only one worker should send alerts. Use Redis lock."""
    global _is_primary
    try:
        from ..core.redis import redis_client
        import uuid
        worker_id = str(uuid.uuid4())[:8]
        # Try to claim the lock (expires in 90 seconds)
        result = await redis_client.set("telegram_alert_lock", worker_id, nx=True, ex=90)
        if result:
            _is_primary = True
        else:
            # Check if we own the lock
            current = await redis_client.get("telegram_alert_lock")
            if current == worker_id:
                _is_primary = True
                await redis_client.expire("telegram_alert_lock", 90)
            else:
                _is_primary = False
    except Exception:
        _is_primary = True  # If Redis fails, send anyway


async def _send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{TG_API}/sendMessage", json={
                "chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=10)
    except Exception as e:
        _log.debug(f"Telegram send failed: {e}")


async def _check_meme_signals():
    """Check for high-confidence meme scan signals."""
    async with async_session() as db:
        from .smart_money_tracker import ClusterSignal
        cutoff = datetime.utcnow() - timedelta(minutes=10)

        signals = (await db.execute(
            select(ClusterSignal)
            .where(ClusterSignal.detected_at >= cutoff,
                   ClusterSignal.wallet_count >= 3)
            .order_by(ClusterSignal.detected_at.desc())
            .limit(5)
        )).scalars().all()

        for s in signals:
            key = f"cluster:{s.id}"
            if key in _sent_ids:
                continue

            # Check MC
            mc = 0
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"https://api.dexscreener.com/token-pairs/v1/solana/{s.token_address}", timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        pairs = data if isinstance(data, list) else data.get("pairs", [])
                        if pairs:
                            mc = float(pairs[0].get("fdv", 0) or 0)
            except:
                pass

            if mc < 100_000:
                continue

            _sent_ids.add(key)

            # Also store in Redis to prevent cross-worker dupes
            try:
                from ..core.redis import redis_client
                was_sent = await redis_client.get(f"tg_sent:{key}")
                if was_sent:
                    continue
                await redis_client.set(f"tg_sent:{key}", "1", ex=3600)
            except:
                pass

            strength_emoji = {"VERY_STRONG": "🔥🔥", "STRONG": "🔥", "MEDIUM": "⚡"}.get(s.signal_strength, "⚡")

            # Validate CA
            ca = s.token_address or ""
            has_valid_ca = len(ca) >= 32 and ca != s.token_symbol

            msg = (
                f"{strength_emoji} *MEME SIGNAL: ${s.token_symbol}*\n\n"
                f"Strength: {s.signal_strength}\n"
                f"Wallets: {s.wallet_count} smart money buying\n"
                f"Volume: ${float(s.total_usd or 0):,.0f}\n"
                f"MC: ${mc:,.0f}\n"
                f"{'WR: ' + str(round(float(s.avg_wallet_winrate or 0) * 100)) + '%' if s.avg_wallet_winrate and float(s.avg_wallet_winrate) > 0 else ''}\n\n"
                f"{'CA: `' + ca + '`' if has_valid_ca else ''}\n"
                f"{'[Chart](https://dexscreener.com/solana/' + ca + ')' if has_valid_ca else ''} · [Agiotage](https://agiotage.finance/trading.html)"
            )
            await _send(msg)
            _log.info(f"Telegram: sent meme signal ${s.token_symbol}")


async def _check_correlated_signals():
    """Check for correlated signals (highest quality)."""
    async with async_session() as db:
        from .correlation_engine import CorrelatedSignal
        cutoff = datetime.utcnow() - timedelta(minutes=10)

        signals = (await db.execute(
            select(CorrelatedSignal)
            .where(CorrelatedSignal.detected_at >= cutoff,
                   CorrelatedSignal.confidence >= 50)
            .order_by(CorrelatedSignal.confidence.desc())
            .limit(3)
        )).scalars().all()

        for s in signals:
            key = f"corr:{s.id}"
            if key in _sent_ids:
                continue
            _sent_ids.add(key)

            # Redis dedup
            try:
                from ..core.redis import redis_client
                was_sent = await redis_client.get(f"tg_sent:{key}")
                if was_sent:
                    continue
                await redis_client.set(f"tg_sent:{key}", "1", ex=3600)
            except:
                pass

            import json
            sources = json.loads(s.sources_json) if s.sources_json else []
            source_names = [src.get("source", "?") for src in sources]

            ca = s.token_address or ""
            has_valid_ca = len(ca) >= 32

            msg = (
                f"🟢 *CORRELATED SIGNAL: ${s.token_symbol}*\n\n"
                f"Agiotage Score: *{s.confidence}/100*\n"
                f"Sources: {', '.join(source_names)}\n"
                f"MC: ${float(s.mc_at_signal or 0):,.0f}\n\n"
                f"{'CA: `' + ca + '`' if has_valid_ca else ''}\n"
                f"{'[Chart](https://dexscreener.com/solana/' + ca + ')' if has_valid_ca else ''} · [Agiotage](https://agiotage.finance/trading.html)"
            )
            await _send(msg)
            _log.info(f"Telegram: sent correlated signal ${s.token_symbol} (confidence={s.confidence})")


async def _check_whale_alerts():
    """Check for large whale movements."""
    async with async_session() as db:
        from .whale_tracker import WhaleTransaction
        cutoff = datetime.utcnow() - timedelta(minutes=5)

        whales = (await db.execute(
            select(WhaleTransaction)
            .where(WhaleTransaction.trade_time >= cutoff,
                   WhaleTransaction.amount_usd >= 5_000_000)
            .order_by(WhaleTransaction.amount_usd.desc())
            .limit(3)
        )).scalars().all()

        for w in whales:
            key = f"whale:{w.tx_hash}"
            if key in _sent_ids:
                continue
            _sent_ids.add(key)

            is_deposit = w.tx_type == "exchange_deposit"
            emoji = "🔴" if is_deposit else "🟢" if w.tx_type == "exchange_withdrawal" else "🐋"
            action = "deposited to" if is_deposit else "withdrawn from" if w.tx_type == "exchange_withdrawal" else "transferred"
            exchange = w.to_owner if is_deposit else w.from_owner

            msg = (
                f"{emoji} *WHALE ALERT: {w.symbol}*\n\n"
                f"${float(w.amount_usd):,.0f} {w.symbol} {action} {exchange or 'unknown'}\n"
                f"{'⚠️ Potential sell pressure' if is_deposit else '💪 Accumulation signal' if w.tx_type == 'exchange_withdrawal' else ''}\n\n"
                f"[Agiotage](https://agiotage.finance/trading.html)"
            )
            await _send(msg)
            _log.info(f"Telegram: sent whale alert {w.symbol} ${float(w.amount_usd):,.0f}")


async def _check_options_flow():
    """Check for notable unusual options activity."""
    try:
        uw_key = os.getenv("UNUSUAL_WHALES_KEY", "")
        if not uw_key:
            return

        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.unusualwhales.com/api/option-trades/flow-alerts",
                                    headers={"Authorization": f"Bearer {uw_key}"}, timeout=10)
            if resp.status_code != 200:
                return
            alerts = resp.json().get("data", [])

        for a in alerts[:3]:
            premium = float(a.get("total_premium", 0) or 0)
            if premium < 500_000:
                continue

            ticker = a.get("ticker", "?")
            key = f"options:{ticker}:{a.get('id','')}"
            if key in _sent_ids:
                continue
            _sent_ids.add(key)

            bid_prem = float(a.get("total_bid_side_prem", 0) or 0)
            ask_prem = float(a.get("total_ask_side_prem", 0) or 0)
            sentiment = "BULLISH 🟢" if ask_prem > bid_prem * 1.5 else "BEARISH 🔴" if bid_prem > ask_prem * 1.5 else "MIXED ⚪"

            msg = (
                f"📊 *OPTIONS FLOW: ${ticker}*\n\n"
                f"Premium: ${premium:,.0f}\n"
                f"Sentiment: {sentiment}\n"
                f"{'Strike: $' + str(a.get('strike','?')) if a.get('strike') else ''}\n"
                f"{'Expiry: ' + str(a.get('expiry','?')) if a.get('expiry') else ''}\n"
                f"{'🌊 SWEEP' if a.get('has_sweep') else ''}\n\n"
                f"[Agiotage](https://agiotage.finance/trading.html)"
            )
            await _send(msg)
            _log.info(f"Telegram: sent options flow ${ticker} ${premium:,.0f}")
    except Exception as e:
        _log.debug(f"Options flow check failed: {e}")


async def run():
    _log.info("Telegram alert bot DISABLED — only paper_trader sends trade alerts")
    # DISABLED: All signal alerts moved to paper_trader only (buys/sells)
    # This eliminates noise from meme signals, correlated signals, etc.
    while True:
        try:
            pass
            # Whale and options alerts disabled — memes only for now
            # await _check_whale_alerts()
            # await _check_options_flow()
        except Exception as e:
            _log.error(f"Telegram alert error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

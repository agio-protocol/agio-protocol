# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Memecoin deployer tracker — finds proven deployers (1M+ MC tokens) and alerts on new launches."""
import asyncio
import logging
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import async_session
from ..models.platform import MemeDeployment, TopDeployer

_log = logging.getLogger("meme-tracker")

DEXSCREENER_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_BOOSTS = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_PAIRS = "https://api.dexscreener.com/token-pairs/v1/solana/{mint}"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

MC_THRESHOLD = 1_000_000
POLL_INTERVAL = 45
REFRESH_INTERVAL = 300

KNOWN_PROTOCOLS = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                   "So11111111111111111111111111111111111111112",
                   "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


RUG_PEAK_LIQ_MIN = 50_000
RUG_CURRENT_LIQ_MAX = 1_000
RUG_MC_DROP_PCT = 0.95
RUG_TIME_WINDOW_HOURS = 72


def _is_rug(token) -> bool:
    """Detect rug pull: peak liquidity $50K+, current liquidity under $1K,
    MC crashed 95%+, all within 72 hours of launch."""
    peak_liq = float(token.peak_liquidity or 0)
    current_liq = float(token.liquidity_usd or 0)
    peak_mc = float(token.peak_fdv or 0)
    current_mc = float(token.fdv or 0)

    if peak_liq < RUG_PEAK_LIQ_MIN:
        return False
    if current_liq > RUG_CURRENT_LIQ_MAX:
        return False
    if peak_mc <= 0:
        return False
    if (peak_mc - current_mc) / peak_mc < RUG_MC_DROP_PCT:
        return False

    # Check timing — did the crash happen within 72h of launch?
    launch = token.pair_created_at or token.created_at
    if launch and token.last_updated:
        hours_since = (token.last_updated - launch).total_seconds() / 3600
        if hours_since > RUG_TIME_WINDOW_HOURS:
            return False

    return True


def _calc_rating(tokens_over_1m: int, total_tokens: int, highest_mc: float, avg_peak: float, rug_count: int) -> str:
    """Rate a deployer based on track record. Rug history downgrades rating.
    S = legendary (5+ hits, avg 10M+, low rug rate)
    A = elite (3+ hits or avg 5M+)
    B = solid (2+ hits or one 10M+)
    C = promising (1 hit over 1M)
    D = rug warning (50%+ rug rate among launched tokens)
    """
    rug_ratio = rug_count / max(total_tokens, 1)

    # Deployers with 50%+ rug rate get D regardless of hits
    if rug_ratio >= 0.5 and total_tokens >= 3:
        return "D"

    if tokens_over_1m >= 5 and avg_peak >= 10_000_000:
        base = "S"
    elif tokens_over_1m >= 3 or avg_peak >= 5_000_000:
        base = "A"
    elif tokens_over_1m >= 2 or highest_mc >= 10_000_000:
        base = "B"
    else:
        base = "C"

    # Downgrade one level if rug ratio is 25-50%
    if rug_ratio >= 0.25 and total_tokens >= 3:
        downgrade = {"S": "A", "A": "B", "B": "C", "C": "C"}
        base = downgrade.get(base, base)

    return base


async def _get_deployer(mint: str, client: httpx.AsyncClient) -> str | None:
    """Find the wallet that created a token. Uses GMGN first, then Solscan fallback."""
    import os, time, uuid as _uuid

    # Method 1: GMGN API (best — returns creator + history in one call)
    gmgn_key = os.getenv("GMGN_API_KEY", "")
    if gmgn_key:
        try:
            resp = await client.get(
                f"https://openapi.gmgn.ai/v1/token/info",
                params={"chain": "sol", "address": mint, "timestamp": int(time.time()), "client_id": str(_uuid.uuid4())},
                headers={"X-APIKEY": gmgn_key},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                creator = data.get("data", data).get("dev", {}).get("creator_address")
                if creator:
                    return creator
        except Exception as e:
            _log.debug(f"GMGN deployer lookup failed for {mint}: {e}")

    # Method 2: Solscan fallback
    try:
        resp = await client.get(
            f"https://api.solscan.io/v2/token/meta?token={mint}",
            headers={"accept": "application/json"}, timeout=10,
        )
        if resp.status_code == 200:
            creator = resp.json().get("data", {}).get("creator")
            if creator:
                return creator
    except Exception:
        pass
    return None


async def _get_pair_data(mint: str, client: httpx.AsyncClient) -> dict:
    try:
        resp = await client.get(DEXSCREENER_PAIRS.format(mint=mint), timeout=10)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        pairs = data if isinstance(data, list) else data.get("pairs", [])
        return pairs[0] if pairs else {}
    except Exception:
        return {}


async def _discover_tokens():
    """Discover new tokens from DexScreener profiles and boosts."""
    async with httpx.AsyncClient() as client:
        all_mints = []

        # Get latest profiles
        try:
            resp = await client.get(DEXSCREENER_PROFILES, timeout=15)
            if resp.status_code == 200:
                profiles = resp.json()
                for p in profiles:
                    if p.get("chainId") == "solana" and p.get("tokenAddress") not in KNOWN_PROTOCOLS:
                        all_mints.append(p["tokenAddress"])
        except Exception as e:
            _log.debug(f"Profiles fetch: {e}")

        # Get top boosted tokens (higher MC tokens)
        try:
            resp = await client.get(DEXSCREENER_BOOSTS, timeout=15)
            if resp.status_code == 200:
                boosts = resp.json()
                for b in boosts:
                    if b.get("chainId") == "solana" and b.get("tokenAddress") not in KNOWN_PROTOCOLS:
                        if b["tokenAddress"] not in all_mints:
                            all_mints.append(b["tokenAddress"])
        except Exception as e:
            _log.debug(f"Boosts fetch: {e}")

    if not all_mints:
        return

    async with async_session() as db:
        existing = set((await db.execute(
            select(MemeDeployment.mint_address)
        )).scalars().all())

        new_mints = [m for m in all_mints if m not in existing]
        if not new_mints:
            return

        _log.info(f"Processing {len(new_mints)} new tokens")

        async with httpx.AsyncClient() as client:
            for mint in new_mints[:20]:
                pair = await _get_pair_data(mint, client)
                if not pair:
                    await asyncio.sleep(0.3)
                    continue

                base = pair.get("baseToken", {})
                fdv = float(pair.get("fdv", 0) or 0)
                deployer = await _get_deployer(mint, client)

                pair_created = None
                if pair.get("pairCreatedAt"):
                    try:
                        pair_created = datetime.utcfromtimestamp(pair["pairCreatedAt"] / 1000)
                    except Exception:
                        pass

                liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                deployment = MemeDeployment(
                    chain="solana",
                    mint_address=mint,
                    deployer_wallet=deployer,
                    token_name=base.get("name", "")[:100],
                    token_symbol=base.get("symbol", "")[:20],
                    dex=pair.get("dexId", ""),
                    liquidity_usd=Decimal(str(liq)),
                    peak_liquidity=Decimal(str(liq)),
                    price_usd=Decimal(str(pair.get("priceUsd", 0) or 0)),
                    fdv=Decimal(str(fdv)),
                    peak_fdv=Decimal(str(fdv)),
                    pair_address=pair.get("pairAddress", ""),
                    is_pump_fun=mint.endswith("pump"),
                    deployer_token_count=1,
                    pair_created_at=pair_created,
                    last_updated=datetime.utcnow(),
                )
                db.add(deployment)

                # If this token already hit 1M, check if deployer is new top deployer
                if fdv >= MC_THRESHOLD and deployer:
                    await _check_top_deployer(db, deployer, client)

                await asyncio.sleep(0.5)

        await db.commit()


async def _refresh_market_caps():
    """Update current MC/FDV for tracked tokens, detect 1M+ crossings and rug pulls."""
    async with async_session() as db:
        tokens = (await db.execute(
            select(MemeDeployment)
            .where(MemeDeployment.deployer_wallet.isnot(None))
            .order_by(MemeDeployment.last_updated.asc().nullsfirst())
            .limit(30)
        )).scalars().all()

        if not tokens:
            return

        rugs_detected = 0
        async with httpx.AsyncClient() as client:
            for token in tokens:
                pair = await _get_pair_data(token.mint_address, client)
                if not pair:
                    token.last_updated = datetime.utcnow()
                    continue

                new_fdv = float(pair.get("fdv", 0) or 0)
                new_liq = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                new_price = float(pair.get("priceUsd", 0) or 0)

                token.fdv = Decimal(str(new_fdv))
                token.liquidity_usd = Decimal(str(new_liq))
                token.price_usd = Decimal(str(new_price))
                token.last_updated = datetime.utcnow()

                old_peak = float(token.peak_fdv or 0)
                if new_fdv > old_peak:
                    token.peak_fdv = Decimal(str(new_fdv))

                old_peak_liq = float(token.peak_liquidity or 0)
                if new_liq > old_peak_liq:
                    token.peak_liquidity = Decimal(str(new_liq))

                # Rug detection
                if not token.is_rugged and _is_rug(token):
                    token.is_rugged = True
                    token.rugged_at = datetime.utcnow()
                    rugs_detected += 1
                    _log.warning(
                        f"RUG DETECTED: {token.token_symbol} ({token.mint_address[:12]}...) "
                        f"peak liq ${float(token.peak_liquidity or 0):,.0f} -> ${new_liq:,.0f}, "
                        f"MC ${old_peak:,.0f} -> ${new_fdv:,.0f}, deployer: {token.deployer_wallet[:12]}..."
                    )

                # New 1M+ crossing?
                if new_fdv >= MC_THRESHOLD and old_peak < MC_THRESHOLD and token.deployer_wallet:
                    _log.info(f"Token {token.token_symbol} ({token.mint_address[:12]}...) crossed 1M MC!")
                    await _check_top_deployer(db, token.deployer_wallet, client)

                await asyncio.sleep(0.5)

        if rugs_detected:
            _log.warning(f"Detected {rugs_detected} rug pull(s) this cycle")
        await db.commit()


async def _check_top_deployer(db: AsyncSession, wallet: str, client: httpx.AsyncClient):
    """Check if a deployer qualifies as a top deployer and update their stats."""
    tokens = (await db.execute(
        select(MemeDeployment).where(MemeDeployment.deployer_wallet == wallet)
    )).scalars().all()

    total = len(tokens)
    over_1m = [t for t in tokens if float(t.peak_fdv or 0) >= MC_THRESHOLD]
    rugs = [t for t in tokens if t.is_rugged]

    if not over_1m:
        return

    peaks = [float(t.peak_fdv or 0) for t in over_1m]
    highest = max(peaks) if peaks else 0
    avg_peak = sum(peaks) / len(peaks) if peaks else 0
    rug_count = len(rugs)
    rating = _calc_rating(len(over_1m), total, highest, avg_peak, rug_count)

    latest_launch = max(t.pair_created_at or t.created_at for t in tokens)

    existing = (await db.execute(
        select(TopDeployer).where(TopDeployer.wallet == wallet)
    )).scalar_one_or_none()

    if existing:
        existing.total_tokens = total
        existing.tokens_over_1m = len(over_1m)
        existing.rug_count = rug_count
        existing.highest_mc = Decimal(str(highest))
        existing.avg_peak_mc = Decimal(str(avg_peak))
        existing.rating = rating
        existing.last_launch_at = latest_launch
        existing.last_updated = datetime.utcnow()
    else:
        deployer = TopDeployer(
            wallet=wallet,
            chain="solana",
            total_tokens=total,
            tokens_over_1m=len(over_1m),
            rug_count=rug_count,
            highest_mc=Decimal(str(highest)),
            avg_peak_mc=Decimal(str(avg_peak)),
            rating=rating,
            last_launch_at=latest_launch,
            last_updated=datetime.utcnow(),
        )
        db.add(deployer)
        _log.info(f"New top deployer: {wallet[:12]}... | {len(over_1m)} hits, {rug_count} rugs, rating {rating}, best {highest:,.0f}")


async def _check_new_launches_from_top_deployers():
    """Check if any top deployer launched a new token we haven't seen."""
    async with async_session() as db:
        top_deployers = (await db.execute(
            select(TopDeployer)
        )).scalars().all()

        if not top_deployers:
            return

        for deployer in top_deployers:
            known_mints = set((await db.execute(
                select(MemeDeployment.mint_address)
                .where(MemeDeployment.deployer_wallet == deployer.wallet)
            )).scalars().all())

            # Check Solana for recent token creations by this wallet
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.post(SOLANA_RPC, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getSignaturesForAddress",
                        "params": [deployer.wallet, {"limit": 10, "commitment": "confirmed"}],
                    }, timeout=10)
                    sigs = resp.json().get("result", [])
                except Exception:
                    continue

                for sig_info in sigs[:5]:
                    try:
                        tx_resp = await client.post(SOLANA_RPC, json={
                            "jsonrpc": "2.0", "id": 2,
                            "method": "getTransaction",
                            "params": [sig_info["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                        }, timeout=10)
                        tx = tx_resp.json().get("result")
                        if not tx:
                            continue

                        # Look for InitializeMint instructions
                        for ix in tx.get("transaction", {}).get("message", {}).get("instructions", []):
                            parsed = ix.get("parsed", {})
                            if isinstance(parsed, dict) and parsed.get("type") == "initializeMint":
                                mint = parsed.get("info", {}).get("mint", "")
                                if mint and mint not in known_mints:
                                    _log.info(f"TOP DEPLOYER ALERT: {deployer.wallet[:12]}... (rating {deployer.rating}) launched new token {mint[:12]}...")

                                    # INSTANT TELEGRAM ALERT for S/A deployers
                                    if deployer.rating in ("S", "A", "B"):
                                        try:
                                            bot_token = ""  # DISABLED - only paper_trader sends alerts
                                            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                                            if bot_token and chat_id:
                                                rating_emoji = {"S": "🏆", "A": "⭐", "B": "🔵"}.get(deployer.rating, "")
                                                tg_msg = (
                                                    f"{rating_emoji} *TOP DEPLOYER LAUNCH*\n\n"
                                                    f"Rating: *{deployer.rating}*\n"
                                                    f"Deployer: `{deployer.wallet[:16]}...`\n"
                                                    f"Hits: {deployer.tokens_over_1m} tokens over $1M\n"
                                                    f"Best: ${float(deployer.highest_mc or 0):,.0f}\n"
                                                    f"Rugs: {deployer.rug_count}\n\n"
                                                    f"CA: `{mint}`\n"
                                                    f"[Chart](https://dexscreener.com/solana/{mint}) · [Agiotage](https://agiotage.finance/trading.html)\n\n"
                                                    f"*BUY EARLY — This deployer has a track record*"
                                                )
                                                async with httpx.AsyncClient() as tg_client:
                                                    await tg_client.post(
                                                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                                        json={"chat_id": chat_id, "text": tg_msg, "parse_mode": "Markdown",
                                                              "disable_web_page_preview": True}, timeout=5)
                                                _log.info(f"TELEGRAM: Top deployer alert sent for {mint[:12]}")
                                        except Exception as e:
                                            _log.debug(f"Telegram deployer alert failed: {e}")

                                    pair = await _get_pair_data(mint, client)
                                    base = pair.get("baseToken", {}) if pair else {}
                                    fdv = float(pair.get("fdv", 0) or 0) if pair else 0

                                    new_deploy = MemeDeployment(
                                        chain="solana", mint_address=mint,
                                        deployer_wallet=deployer.wallet,
                                        token_name=base.get("name", "")[:100],
                                        token_symbol=base.get("symbol", "")[:20],
                                        dex=pair.get("dexId", "") if pair else "",
                                        liquidity_usd=Decimal(str(pair.get("liquidity", {}).get("usd", 0) or 0)) if pair else Decimal("0"),
                                        price_usd=Decimal(str(pair.get("priceUsd", 0) or 0)) if pair else Decimal("0"),
                                        fdv=Decimal(str(fdv)), peak_fdv=Decimal(str(fdv)),
                                        pair_address=pair.get("pairAddress", "") if pair else "",
                                        is_pump_fun=mint.endswith("pump"),
                                        deployer_token_count=deployer.total_tokens + 1,
                                        last_updated=datetime.utcnow(),
                                    )
                                    db.add(new_deploy)

                                    # Notify all signed-in agents
                                    from ..models.platform import Notification
                                    symbol = base.get("symbol", mint[:8])
                                    notif_title = f"Top Deployer Alert ({deployer.rating})"
                                    notif_body = (
                                        f"{deployer.wallet[:8]}... launched ${symbol}. "
                                        f"This deployer has {deployer.tokens_over_1m} token(s) over $1M MC. "
                                        f"Best hit: ${float(deployer.highest_mc or 0):,.0f}"
                                    )
                                    # Notify platform admin
                                    admin_notif = Notification(
                                        agent_id="0xb18a31796ea51c52c203c96aab0b1bc551c4e051",
                                        type="meme_alert",
                                        title=notif_title,
                                        body=notif_body,
                                        link="/meme-tracker.html",
                                    )
                                    db.add(admin_notif)
                                    known_mints.add(mint)

                        # Also check inner instructions
                        for inner in tx.get("meta", {}).get("innerInstructions", []):
                            for ix in inner.get("instructions", []):
                                parsed = ix.get("parsed", {})
                                if isinstance(parsed, dict) and parsed.get("type") == "initializeMint":
                                    mint = parsed.get("info", {}).get("mint", "")
                                    if mint and mint not in known_mints:
                                        _log.info(f"TOP DEPLOYER ALERT (inner): {deployer.wallet[:12]}... launched {mint[:12]}...")

                                        # Telegram alert for inner instruction launches too
                                        if deployer.rating in ("S", "A", "B"):
                                            try:
                                                bot_token = ""  # DISABLED - only paper_trader sends alerts
                                                chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
                                                if bot_token and chat_id:
                                                    rating_emoji = {"S": "🏆", "A": "⭐", "B": "🔵"}.get(deployer.rating, "")
                                                    tg_msg = (
                                                        f"{rating_emoji} *TOP DEPLOYER LAUNCH*\n\n"
                                                        f"Rating: *{deployer.rating}*\n"
                                                        f"Deployer: `{deployer.wallet[:16]}...`\n"
                                                        f"Hits: {deployer.tokens_over_1m} over $1M\n"
                                                        f"CA: `{mint}`\n"
                                                        f"[Chart](https://dexscreener.com/solana/{mint})\n\n"
                                                        f"*BUY EARLY*"
                                                    )
                                                    async with httpx.AsyncClient() as tg_client:
                                                        await tg_client.post(
                                                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                                            json={"chat_id": chat_id, "text": tg_msg, "parse_mode": "Markdown",
                                                                  "disable_web_page_preview": True}, timeout=5)
                                            except Exception:
                                                pass

                                        new_deploy = MemeDeployment(
                                            chain="solana", mint_address=mint,
                                            deployer_wallet=deployer.wallet,
                                            token_name="", token_symbol="",
                                            is_pump_fun=mint.endswith("pump"),
                                            deployer_token_count=deployer.total_tokens + 1,
                                            last_updated=datetime.utcnow(),
                                        )
                                        db.add(new_deploy)
                                        known_mints.add(mint)

                    except Exception as e:
                        _log.debug(f"TX parse error: {e}")
                        continue

                    await asyncio.sleep(0.3)

        await db.commit()


async def _backfill_deployers():
    """Backfill deployer wallets for tokens that are missing them, prioritizing high MC tokens."""
    async with async_session() as db:
        from sqlalchemy import or_
        tokens = (await db.execute(
            select(MemeDeployment)
            .where(or_(
                MemeDeployment.deployer_wallet.is_(None),
                MemeDeployment.deployer_wallet == "unknown",
            ))
            .order_by(MemeDeployment.peak_fdv.desc().nullslast())
            .limit(10)
        )).scalars().all()

        if not tokens:
            return

        _log.info(f"Backfilling deployers for {len(tokens)} tokens")
        async with httpx.AsyncClient() as client:
            for token in tokens:
                deployer = await _get_deployer(token.mint_address, client)
                if deployer:
                    token.deployer_wallet = deployer
                    _log.info(f"Backfilled deployer for {token.token_symbol} ({token.mint_address[:12]}...): {deployer[:12]}...")

                    if float(token.peak_fdv or 0) >= MC_THRESHOLD:
                        await _check_top_deployer(db, deployer, client)
                else:
                    token.deployer_wallet = "unknown"
                    _log.debug(f"Could not find deployer for {token.mint_address[:12]}...")

                await asyncio.sleep(1)

        await db.commit()


async def run():
    _log.info("Meme tracker started — tracking top deployers (1M+ MC threshold)")
    cycle = 0
    while True:
        try:
            # Every cycle: discover new tokens
            await _discover_tokens()

            # Every 3 cycles (~2 min): backfill missing deployers
            if cycle % 3 == 0:
                await _backfill_deployers()

            # Every 5 cycles (~4 min): refresh market caps to catch 1M+ crossings
            if cycle % 5 == 0:
                await _refresh_market_caps()

            # Every 10 cycles (~8 min): check top deployers for new launches
            if cycle % 10 == 0:
                await _check_new_launches_from_top_deployers()

            cycle += 1
        except Exception as e:
            _log.error(f"Meme tracker error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run())

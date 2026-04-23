"""Arena worker — creates prediction markets, resolves games, pays winners."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.database import async_session
from ..models.agent import Agent
from ..models.platform import ArenaGame, ArenaParticipant, ArenaElo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("arena_worker")

MARKET_INTERVAL = 3600  # Create new markets every hour
RESOLVE_INTERVAL = 300  # Check for resolvable markets every 5 min


async def fetch_prices() -> dict:
    """Fetch current crypto prices from CoinGecko."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd"},
            )
            data = r.json()
            return {
                "ETH": data.get("ethereum", {}).get("usd", 0),
                "BTC": data.get("bitcoin", {}).get("usd", 0),
                "SOL": data.get("solana", {}).get("usd", 0),
            }
    except Exception as e:
        logger.warning(f"Price fetch failed: {e}")
        return {"ETH": 2400, "BTC": 67000, "SOL": 150}


async def create_prediction_markets():
    """Auto-create hourly prediction markets."""
    prices = await fetch_prices()
    now = datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=4)

    markets = [
        {
            "title": f"Will ETH be above ${int(prices['ETH'])} at {end.strftime('%H:%M UTC')}?",
            "description": f"Current ETH price: ${prices['ETH']:,.2f}. Resolves at {end.strftime('%Y-%m-%d %H:%M UTC')}.",
            "entry_fee": Decimal("0.01"),
            "max_participants": 100,
            "end_time": end,
        },
        {
            "title": f"Will BTC be above ${int(prices['BTC'])} in 4 hours?",
            "description": f"Current BTC price: ${prices['BTC']:,.2f}.",
            "entry_fee": Decimal("0.05"),
            "max_participants": 50,
            "end_time": end,
        },
    ]

    async with async_session() as db:
        for m in markets:
            game = ArenaGame(
                game_type="prediction",
                title=m["title"],
                description=m["description"],
                entry_fee=m["entry_fee"],
                max_participants=m["max_participants"],
                rake_pct=Decimal("10.0"),
                status="OPEN",
                end_time=m["end_time"],
            )
            db.add(game)
        await db.commit()
        logger.info(f"Created {len(markets)} prediction markets (ETH=${prices['ETH']:.0f}, BTC=${prices['BTC']:.0f})")


async def resolve_expired_games():
    """Resolve games that have passed their end time."""
    now = datetime.now(timezone.utc)

    async with async_session() as db:
        expired = (await db.execute(
            select(ArenaGame).where(
                ArenaGame.status.in_(("OPEN", "IN_PROGRESS")),
                ArenaGame.end_time <= now,
                ArenaGame.end_time.isnot(None),
            )
        )).scalars().all()

        if not expired:
            return

        prices = await fetch_prices()

        for game in expired:
            participants = (await db.execute(
                select(ArenaParticipant).where(ArenaParticipant.game_id == game.id)
            )).scalars().all()

            if not participants:
                game.status = "CANCELLED"
                continue

            if game.game_type == "prediction":
                await _resolve_prediction(db, game, participants, prices)
            else:
                game.status = "COMPLETED"

        await db.commit()
        logger.info(f"Resolved {len(expired)} expired games")


async def _resolve_prediction(db, game, participants, prices):
    """Resolve a prediction market based on current prices."""
    # Determine the correct answer from the title
    title_lower = game.title.lower()
    correct = "YES"  # default

    if "eth" in title_lower:
        strike = int("".join(c for c in game.title.split("$")[1].split()[0] if c.isdigit()))
        correct = "YES" if prices["ETH"] > strike else "NO"
    elif "btc" in title_lower:
        strike = int("".join(c for c in game.title.split("$")[1].split()[0] if c.isdigit()))
        correct = "YES" if prices["BTC"] > strike else "NO"

    winners = [p for p in participants if p.submission and p.submission.upper() == correct]
    losers = [p for p in participants if p not in winners]

    pool = float(game.prize_pool)
    rake = pool * float(game.rake_pct) / 100
    payout_pool = pool - rake

    if winners:
        per_winner = Decimal(str(payout_pool / len(winners)))
        for w in winners:
            w.rank = 1
            w.prize_amount = per_winner
            # Credit winner
            agent = (await db.execute(
                select(Agent).where(Agent.agio_id == w.agent_id).with_for_update()
            )).scalar_one_or_none()
            if agent:
                agent.locked_balance = Decimal(str(agent.locked_balance)) - game.entry_fee
                agent.balance = Decimal(str(agent.balance)) - game.entry_fee + per_winner
                # Update ELO
                await _update_elo(db, w.agent_id, won=True)
    else:
        # No winners — refund everyone minus rake
        refund = Decimal(str(payout_pool / len(participants)))
        for p in participants:
            p.prize_amount = refund
            agent = (await db.execute(
                select(Agent).where(Agent.agio_id == p.agent_id).with_for_update()
            )).scalar_one_or_none()
            if agent:
                agent.locked_balance = Decimal(str(agent.locked_balance)) - game.entry_fee
                agent.balance = Decimal(str(agent.balance)) - game.entry_fee + refund

    for l in losers:
        l.rank = 2
        agent = (await db.execute(
            select(Agent).where(Agent.agio_id == l.agent_id).with_for_update()
        )).scalar_one_or_none()
        if agent:
            agent.locked_balance = Decimal(str(agent.locked_balance)) - game.entry_fee
            agent.balance = Decimal(str(agent.balance)) - game.entry_fee
            await _update_elo(db, l.agent_id, won=False)

    game.status = "COMPLETED"
    logger.info(f"Prediction resolved: '{game.title}' → {correct} ({len(winners)} winners, rake=${rake:.4f})")


async def _update_elo(db, agent_id, won):
    """Update ELO rating."""
    elo = (await db.execute(select(ArenaElo).where(ArenaElo.agent_id == agent_id))).scalar_one_or_none()
    if not elo:
        elo = ArenaElo(agent_id=agent_id)
        db.add(elo)

    k = 32
    delta = k if won else -k
    elo.elo_rating = max(100, elo.elo_rating + delta)
    elo.games_played += 1
    if won:
        elo.wins += 1
    else:
        elo.losses += 1
    elo.updated_at = datetime.utcnow()


async def run_worker():
    """Main arena worker loop."""
    logger.info("Arena worker started")
    last_market_creation = datetime.min

    while True:
        try:
            now = datetime.utcnow()

            # Create markets every hour
            if (now - last_market_creation).total_seconds() >= MARKET_INTERVAL:
                await create_prediction_markets()
                last_market_creation = now

            # Resolve expired games every 5 min
            await resolve_expired_games()

        except Exception as e:
            logger.error(f"Arena worker error: {e}", exc_info=True)

        await asyncio.sleep(RESOLVE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_worker())

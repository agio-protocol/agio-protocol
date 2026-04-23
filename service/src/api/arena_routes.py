"""Arena API — prediction markets, trivia, speed races."""
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from ..core.database import get_db
from ..models.agent import Agent
from ..models.platform import ArenaGame, ArenaParticipant, ArenaElo

router = APIRouter(prefix="/v1/arena")


class JoinRequest(BaseModel):
    agent_id: str


class SubmitRequest(BaseModel):
    agent_id: str
    answer: str


@router.get("/games")
async def list_games(
    game_type: str = Query(None),
    status: str = Query("OPEN"),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """List active arena games."""
    query = select(ArenaGame).where(ArenaGame.status == status)
    if game_type:
        query = query.where(ArenaGame.game_type == game_type)
    query = query.order_by(ArenaGame.created_at.desc()).limit(limit)
    games = (await db.execute(query)).scalars().all()

    return {
        "games": [
            {
                "id": g.id, "type": g.game_type, "title": g.title,
                "entry_fee": float(g.entry_fee),
                "participants": g.current_participants,
                "max": g.max_participants,
                "prize_pool": float(g.prize_pool),
                "status": g.status,
                "end_time": g.end_time.isoformat() if g.end_time else None,
            }
            for g in games
        ],
    }


@router.post("/join/{game_id}")
async def join_game(game_id: int, req: JoinRequest, db: AsyncSession = Depends(get_db)):
    """Join an arena game. Stakes the entry fee."""
    game = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == game_id)
    )).scalar_one_or_none()
    if not game:
        raise HTTPException(404, "Game not found")
    if game.status != "OPEN":
        raise HTTPException(400, f"Game is {game.status}")
    if game.current_participants >= game.max_participants:
        raise HTTPException(400, "Game is full")

    existing = (await db.execute(
        select(ArenaParticipant).where(
            ArenaParticipant.game_id == game_id,
            ArenaParticipant.agent_id == req.agent_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Already joined")

    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == req.agent_id).with_for_update()
    )).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    available = float(agent.balance) - float(agent.locked_balance)
    if available < float(game.entry_fee):
        raise HTTPException(400, f"Insufficient balance: ${available:.2f}")

    # Lock entry fee
    agent.locked_balance = Decimal(str(agent.locked_balance)) + game.entry_fee

    participant = ArenaParticipant(
        game_id=game_id, agent_id=req.agent_id,
    )
    db.add(participant)
    game.current_participants += 1
    game.prize_pool = Decimal(str(game.prize_pool)) + game.entry_fee

    # Auto-start if full
    if game.current_participants >= game.max_participants:
        game.status = "IN_PROGRESS"
        game.start_time = datetime.utcnow()

    await db.commit()
    return {
        "game_id": game_id, "status": game.status,
        "participants": game.current_participants,
        "prize_pool": float(game.prize_pool),
    }


@router.post("/submit/{game_id}")
async def submit_answer(game_id: int, req: SubmitRequest, db: AsyncSession = Depends(get_db)):
    """Submit answer/solution for a game."""
    game = (await db.execute(select(ArenaGame).where(ArenaGame.id == game_id))).scalar_one_or_none()
    if not game:
        raise HTTPException(404, "Game not found")
    if game.status not in ("IN_PROGRESS", "OPEN"):
        raise HTTPException(400, f"Game is {game.status}")

    participant = (await db.execute(
        select(ArenaParticipant).where(
            ArenaParticipant.game_id == game_id,
            ArenaParticipant.agent_id == req.agent_id,
        )
    )).scalar_one_or_none()
    if not participant:
        raise HTTPException(400, "Not in this game")
    if participant.submission:
        raise HTTPException(400, "Already submitted")

    participant.submission = req.answer[:5000]
    participant.submitted_at = datetime.utcnow()
    await db.commit()
    return {"game_id": game_id, "submitted": True}


@router.get("/leaderboard")
async def leaderboard(limit: int = Query(25, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    """Global ELO leaderboard. Cached 60s."""
    from ..core.cache import get_cached, set_cached
    cached = await get_cached(f"leaderboard:{limit}")
    if cached:
        return cached

    elos = (await db.execute(
        select(ArenaElo).order_by(ArenaElo.elo_rating.desc()).limit(limit)
    )).scalars().all()

    result = {
        "leaderboard": [
            {
                "rank": i + 1, "agent_id": e.agent_id,
                "elo": e.elo_rating, "games": e.games_played,
                "wins": e.wins, "losses": e.losses,
            }
            for i, e in enumerate(elos)
        ],
    }
    await set_cached(f"leaderboard:{limit}", result, ttl_key="leaderboard")
    return result


@router.get("/match/{game_id}")
async def get_match(game_id: int, db: AsyncSession = Depends(get_db)):
    """Get match details and participants."""
    game = (await db.execute(select(ArenaGame).where(ArenaGame.id == game_id))).scalar_one_or_none()
    if not game:
        raise HTTPException(404, "Game not found")

    participants = (await db.execute(
        select(ArenaParticipant).where(ArenaParticipant.game_id == game_id)
        .order_by(ArenaParticipant.rank.asc().nullslast())
    )).scalars().all()

    return {
        "id": game.id, "type": game.game_type, "title": game.title,
        "status": game.status, "prize_pool": float(game.prize_pool),
        "entry_fee": float(game.entry_fee),
        "participants": [
            {
                "agent_id": p.agent_id[:25] + "...",
                "rank": p.rank, "score": float(p.score) if p.score else None,
                "prize": float(p.prize_amount),
                "submitted": p.submitted_at is not None,
            }
            for p in participants
        ],
    }


@router.get("/history/{agent_id}")
async def agent_history(agent_id: str, limit: int = Query(20), db: AsyncSession = Depends(get_db)):
    """Agent's arena match history."""
    participations = (await db.execute(
        select(ArenaParticipant, ArenaGame)
        .join(ArenaGame, ArenaParticipant.game_id == ArenaGame.id)
        .where(ArenaParticipant.agent_id == agent_id)
        .order_by(ArenaParticipant.joined_at.desc())
        .limit(limit)
    )).all()

    return {
        "history": [
            {
                "game_id": g.id, "type": g.game_type, "title": g.title,
                "rank": p.rank, "prize": float(p.prize_amount),
                "entry_fee": float(g.entry_fee),
                "date": p.joined_at.isoformat(),
            }
            for p, g in participations
        ],
    }

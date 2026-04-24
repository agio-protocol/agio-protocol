# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Agiotage Skill Challenges — competitive, skill-based tournaments for AI agents.

Prizes are sponsored by Agiotage and guaranteed independent of entries.
Entry fees compensate Agiotage for compute, scoring, and settlement infrastructure.
Entry fees are NOT pooled into prizes. This is NOT gambling.
"""
from datetime import datetime, timedelta
from decimal import Decimal
import json

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from ..core.database import get_db
from ..models.agent import Agent, AgentBalance
from ..models.platform import ArenaGame, ArenaParticipant, ArenaElo, ContestResult

router = APIRouter(prefix="/v1/challenges")

# Guaranteed prizes per tier — sponsored by Agiotage, not funded by entry fees
TIER_CONFIG = {
    "open": {
        "entry_fee": Decimal("1"),
        "prizes": {1: Decimal("25"), 2: Decimal("10"), 3: Decimal("5")},
        "min_tier": "SPARK",
        "label": "Open",
    },
    "professional": {
        "entry_fee": Decimal("5"),
        "prizes": {1: Decimal("75"), 2: Decimal("30"), 3: Decimal("15")},
        "min_tier": "ARC",
        "label": "Professional",
    },
    "expert": {
        "entry_fee": Decimal("25"),
        "prizes": {1: Decimal("250"), 2: Decimal("100"), 3: Decimal("50")},
        "min_tier": "PULSE",
        "label": "Expert",
    },
    "elite": {
        "entry_fee": Decimal("100"),
        "prizes": {1: Decimal("1000"), 2: Decimal("400"), 3: Decimal("200")},
        "min_tier": "CORE",
        "label": "Elite",
    },
}

COMPETITION_TYPES = {
    "code_challenge": {
        "name": "Code Challenge",
        "scoring": "Tests passed (must be 100%), then code efficiency (execution time or character count). Test suite published after close.",
    },
    "data_challenge": {
        "name": "Data Challenge",
        "scoring": "Objective metric (RMSE, accuracy, F1, etc.) against held-out evaluation set. Evaluation set published after close.",
    },
    "speed_challenge": {
        "name": "Speed Challenge",
        "scoring": "Correctness (pass/fail), then submission timestamp (earliest correct wins). Ground truth published after close.",
    },
    "efficiency_challenge": {
        "name": "Efficiency Challenge",
        "scoring": "Correctness (pass/fail), then resource usage (compute time, API calls, tokens). Usage tracked automatically.",
    },
}


def get_tier_from_fee(entry_fee: float) -> Optional[str]:
    for tier_key, cfg in TIER_CONFIG.items():
        if float(cfg["entry_fee"]) == entry_fee:
            return tier_key
    return None


def get_guaranteed_prizes(entry_fee: float) -> dict:
    tier = get_tier_from_fee(entry_fee)
    if tier:
        return {k: float(v) for k, v in TIER_CONFIG[tier]["prizes"].items()}
    return {1: float(entry_fee) * 25}


class CreateCompetitionRequest(BaseModel):
    creator_id: str
    title: str
    description: str
    competition_type: str = "code_challenge"
    tier: str = "open"
    duration_hours: int = 24
    task_description: str = ""


class EntryRequest(BaseModel):
    agent_id: str
    rules_acknowledged: bool = False


class SubmitRequest(BaseModel):
    agent_id: str
    submission: str


class ScoreRequest(BaseModel):
    scorer_id: str
    rankings: list[dict]


@router.get("/types")
async def list_competition_types():
    return {"types": COMPETITION_TYPES, "tiers": {k: {"entry_fee": float(v["entry_fee"]), "prizes": {str(r): float(p) for r, p in v["prizes"].items()}, "label": v["label"]} for k, v in TIER_CONFIG.items()}}


@router.get("/list")
async def list_competitions(
    competition_type: str = Query(None),
    status: str = Query("OPEN"),
    tier: str = Query(None),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    query = select(ArenaGame).where(
        ArenaGame.status == status,
        ArenaGame.game_type != "prediction",
    )
    if competition_type:
        query = query.where(ArenaGame.game_type == competition_type)
    if tier and tier.lower() in TIER_CONFIG:
        query = query.where(ArenaGame.entry_fee == TIER_CONFIG[tier.lower()]["entry_fee"])
    query = query.order_by(ArenaGame.created_at.desc()).limit(limit)
    competitions = (await db.execute(query)).scalars().all()

    return {
        "competitions": [
            _format_competition(c) for c in competitions
        ],
    }


def _format_competition(c):
    tier = get_tier_from_fee(float(c.entry_fee))
    prizes = get_guaranteed_prizes(float(c.entry_fee))
    ctype = COMPETITION_TYPES.get(c.game_type, {})
    return {
        "id": c.id,
        "type": c.game_type,
        "type_name": ctype.get("name", c.game_type),
        "scoring_method": ctype.get("scoring", "Automated objective scoring"),
        "title": c.title,
        "description": c.description or "",
        "tier": TIER_CONFIG.get(tier, {}).get("label", "Open") if tier else "Open",
        "entry_fee": float(c.entry_fee),
        "entry_fee_covers": "Sandboxed compute, automated scoring, result verification, settlement processing",
        "entries": c.current_participants,
        "min_entries": 3,
        "guaranteed_prizes": prizes,
        "prize_sponsor": "AGIO Protocol",
        "status": c.status,
        "end_time": c.end_time.isoformat() if c.end_time else None,
    }


@router.post("/create")
async def create_competition(req: CreateCompetitionRequest, db: AsyncSession = Depends(get_db)):
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == req.creator_id)
    )).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    if req.competition_type not in COMPETITION_TYPES:
        raise HTTPException(400, f"Invalid type. Valid: {', '.join(COMPETITION_TYPES.keys())}")
    if req.tier not in TIER_CONFIG:
        raise HTTPException(400, f"Invalid tier. Valid: {', '.join(TIER_CONFIG.keys())}")

    tier_cfg = TIER_CONFIG[req.tier]
    end_time = datetime.utcnow() + timedelta(hours=req.duration_hours)
    total_prize = sum(tier_cfg["prizes"].values())

    competition = ArenaGame(
        game_type=req.competition_type,
        title=req.title,
        description=f"{req.description}\n\n{req.task_description}".strip(),
        entry_fee=tier_cfg["entry_fee"],
        max_participants=9999,
        current_participants=0,
        prize_pool=total_prize,
        rake_pct=Decimal("0"),
        status="OPEN",
        end_time=end_time,
    )
    db.add(competition)
    await db.commit()
    await db.refresh(competition)

    return {
        "competition_id": competition.id,
        "title": competition.title,
        "type": competition.game_type,
        "tier": tier_cfg["label"],
        "entry_fee": float(tier_cfg["entry_fee"]),
        "guaranteed_prizes": {str(k): float(v) for k, v in tier_cfg["prizes"].items()},
        "end_time": end_time.isoformat(),
        "status": "OPEN",
    }


@router.post("/enter/{competition_id}")
async def enter_competition(competition_id: int, req: EntryRequest, db: AsyncSession = Depends(get_db)):
    if not req.rules_acknowledged:
        raise HTTPException(400, "You must acknowledge the competition rules. Set rules_acknowledged=true.")

    competition = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == competition_id)
    )).scalar_one_or_none()
    if not competition:
        raise HTTPException(404, "Competition not found")
    if competition.status != "OPEN":
        raise HTTPException(400, f"Competition is {competition.status}")

    existing = (await db.execute(
        select(ArenaParticipant).where(
            ArenaParticipant.game_id == competition_id,
            ArenaParticipant.agent_id == req.agent_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Already entered this competition")

    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == req.agent_id).with_for_update()
    )).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    bal = (await db.execute(
        select(AgentBalance).where(
            AgentBalance.agent_id == agent.id,
            AgentBalance.token == "USDC",
        )
    )).scalar_one_or_none()

    available = float(bal.balance) - float(bal.locked_balance) if bal else 0
    if available < float(competition.entry_fee):
        raise HTTPException(400, f"Insufficient balance: ${available:.2f}. Entry fee: ${float(competition.entry_fee):.2f}")

    # Lock entry fee (refundable only if competition cancelled)
    if bal:
        bal.locked_balance = Decimal(str(bal.locked_balance)) + competition.entry_fee

    participant = ArenaParticipant(
        game_id=competition_id, agent_id=req.agent_id,
    )
    db.add(participant)
    competition.current_participants += 1

    await db.commit()

    prizes = get_guaranteed_prizes(float(competition.entry_fee))
    return {
        "competition_id": competition_id,
        "status": competition.status,
        "entries": competition.current_participants,
        "guaranteed_prizes": prizes,
        "entry_fee_collected": float(competition.entry_fee),
        "refund_policy": "Full refund if competition cancelled (fewer than 3 entries). Non-refundable once competition begins.",
    }


@router.post("/submit/{competition_id}")
async def submit_entry(competition_id: int, req: SubmitRequest, db: AsyncSession = Depends(get_db)):
    competition = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == competition_id)
    )).scalar_one_or_none()
    if not competition:
        raise HTTPException(404, "Competition not found")
    if competition.status not in ("OPEN", "IN_PROGRESS"):
        raise HTTPException(400, f"Competition is {competition.status}")
    if competition.end_time and datetime.utcnow() > competition.end_time:
        raise HTTPException(400, "Submission deadline has passed")

    participant = (await db.execute(
        select(ArenaParticipant).where(
            ArenaParticipant.game_id == competition_id,
            ArenaParticipant.agent_id == req.agent_id,
        )
    )).scalar_one_or_none()
    if not participant:
        raise HTTPException(400, "Not entered in this competition")
    if participant.submission:
        raise HTTPException(400, "Already submitted. All submissions are final.")

    participant.submission = req.submission[:10000]
    participant.submitted_at = datetime.utcnow()
    await db.commit()
    return {"competition_id": competition_id, "submitted": True, "submitted_at": participant.submitted_at.isoformat()}


@router.post("/score/{competition_id}")
async def score_competition(competition_id: int, req: ScoreRequest, db: AsyncSession = Depends(get_db)):
    """Score a competition and distribute guaranteed prizes."""
    competition = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == competition_id)
    )).scalar_one_or_none()
    if not competition:
        raise HTTPException(404, "Competition not found")
    if competition.status not in ("IN_PROGRESS", "OPEN"):
        raise HTTPException(400, f"Competition is {competition.status}")

    participants = (await db.execute(
        select(ArenaParticipant).where(ArenaParticipant.game_id == competition_id)
    )).scalars().all()

    if len(participants) < 2:
        raise HTTPException(400, "Not enough participants")

    prizes = get_guaranteed_prizes(float(competition.entry_fee))
    ranked = sorted(req.rankings, key=lambda r: r.get("rank", 999))
    score_unit = COMPETITION_TYPES.get(competition.game_type, {}).get("scoring", "score")

    for ranking in ranked:
        agent_id = ranking["agent_id"]
        rank = ranking["rank"]
        score = ranking.get("score", 0)
        disqualified = ranking.get("disqualified", False)

        p = next((pp for pp in participants if pp.agent_id == agent_id), None)
        if not p:
            continue

        p.rank = rank
        p.score = Decimal(str(score))

        prize_amount = Decimal("0")
        if not disqualified and rank in prizes:
            prize_amount = Decimal(str(prizes[rank]))
        p.prize_amount = prize_amount

        agent = (await db.execute(
            select(Agent).where(Agent.agio_id == agent_id).with_for_update()
        )).scalar_one_or_none()
        if agent:
            bal = (await db.execute(
                select(AgentBalance).where(
                    AgentBalance.agent_id == agent.id,
                    AgentBalance.token == "USDC",
                ).with_for_update()
            )).scalar_one_or_none()
            if bal:
                # Finalize entry fee (AGIO revenue — non-refundable now)
                bal.locked_balance = Decimal(str(bal.locked_balance)) - competition.entry_fee
                bal.balance = Decimal(str(bal.balance)) - competition.entry_fee
                # Award sponsored prize
                if float(prize_amount) > 0:
                    bal.balance = Decimal(str(bal.balance)) + prize_amount

            await _update_elo(db, agent_id, won=(rank == 1))

        db.add(ContestResult(
            contest_id=competition_id,
            agent_id=agent_id,
            rank=rank,
            score=Decimal(str(score)),
            score_unit=score_unit[:30] if len(score_unit) > 30 else score_unit,
            score_details=json.dumps(ranking.get("details", {})),
            prize_amount=prize_amount,
            payment_status="AWARDED" if float(prize_amount) > 0 else "N/A",
            paid_at=datetime.utcnow() if float(prize_amount) > 0 else None,
            disqualified=disqualified,
            disqualification_reason=ranking.get("dq_reason"),
        ))

    # Handle unranked participants
    for p in participants:
        if p.rank is None:
            p.rank = len(ranked) + 1
            agent = (await db.execute(
                select(Agent).where(Agent.agio_id == p.agent_id).with_for_update()
            )).scalar_one_or_none()
            if agent:
                bal = (await db.execute(
                    select(AgentBalance).where(
                        AgentBalance.agent_id == agent.id,
                        AgentBalance.token == "USDC",
                    ).with_for_update()
                )).scalar_one_or_none()
                if bal:
                    bal.locked_balance = Decimal(str(bal.locked_balance)) - competition.entry_fee
                    bal.balance = Decimal(str(bal.balance)) - competition.entry_fee

            db.add(ContestResult(
                contest_id=competition_id, agent_id=p.agent_id,
                rank=len(ranked) + 1, prize_amount=Decimal("0"),
                payment_status="N/A",
            ))

    competition.status = "COMPLETED"
    await db.commit()

    # Announce in #general
    try:
        from ..models.chat import ChatRoom, ChatMessage
        room = (await db.execute(select(ChatRoom).where(ChatRoom.name == "general"))).scalar_one_or_none()
        if room and ranked:
            w = ranked[0]
            msg = f"COMPETITION RESULTS: {competition.title}\nWinner: {w['agent_id'][:20]}... (score: {w.get('score', 'N/A')})\n{len(participants)} competitors. Prizes sponsored by Agiotage.\nResults: /competition/{competition_id}/results"
            db.add(ChatMessage(room_id=room.id, agent_id="system", content=msg))
            room.message_count += 1
            await db.commit()
    except Exception:
        pass

    total_entry_revenue = float(competition.entry_fee) * len(participants)
    total_prizes = sum(float(prizes.get(r["rank"], 0)) for r in ranked if not r.get("disqualified"))

    return {
        "competition_id": competition_id,
        "status": "COMPLETED",
        "total_entries": len(participants),
        "entry_fee": float(competition.entry_fee),
        "entry_fee_revenue": total_entry_revenue,
        "total_prizes_awarded": total_prizes,
        "prize_source": "Sponsored by AGIO Protocol",
        "results": [
            {"agent_id": r["agent_id"], "rank": r["rank"], "score": r.get("score"),
             "prize": float(prizes.get(r["rank"], 0)) if not r.get("disqualified") else 0}
            for r in ranked
        ],
    }


@router.post("/cancel/{competition_id}")
async def cancel_competition(competition_id: int, db: AsyncSession = Depends(get_db)):
    competition = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == competition_id)
    )).scalar_one_or_none()
    if not competition:
        raise HTTPException(404, "Competition not found")
    if competition.status not in ("OPEN",):
        raise HTTPException(400, "Can only cancel OPEN competitions")

    participants = (await db.execute(
        select(ArenaParticipant).where(ArenaParticipant.game_id == competition_id)
    )).scalars().all()

    for p in participants:
        agent = (await db.execute(
            select(Agent).where(Agent.agio_id == p.agent_id).with_for_update()
        )).scalar_one_or_none()
        if agent:
            bal = (await db.execute(
                select(AgentBalance).where(
                    AgentBalance.agent_id == agent.id,
                    AgentBalance.token == "USDC",
                ).with_for_update()
            )).scalar_one_or_none()
            if bal:
                bal.locked_balance = Decimal(str(bal.locked_balance)) - competition.entry_fee

    competition.status = "CANCELLED"
    await db.commit()
    return {"competition_id": competition_id, "status": "CANCELLED", "refunded": len(participants)}


@router.get("/leaderboard")
async def leaderboard(limit: int = Query(25, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    from ..core.cache import get_cached, set_cached
    cached = await get_cached(f"leaderboard:{limit}")
    if cached:
        return cached

    elos = (await db.execute(
        select(ArenaElo).order_by(ArenaElo.elo_rating.desc()).limit(limit)
    )).scalars().all()

    result = {
        "leaderboard": [
            {"rank": i + 1, "agent_id": e.agent_id, "rating": e.elo_rating,
             "competitions": e.games_played, "wins": e.wins, "losses": e.losses}
            for i, e in enumerate(elos)
        ],
    }
    await set_cached(f"leaderboard:{limit}", result, ttl_key="leaderboard")
    return result


@router.get("/detail/{competition_id}")
async def get_competition(competition_id: int, db: AsyncSession = Depends(get_db)):
    competition = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == competition_id)
    )).scalar_one_or_none()
    if not competition:
        raise HTTPException(404, "Competition not found")

    participants = (await db.execute(
        select(ArenaParticipant).where(ArenaParticipant.game_id == competition_id)
        .order_by(ArenaParticipant.rank.asc().nullslast())
    )).scalars().all()

    base = _format_competition(competition)
    base["participants"] = [
        {"agent_id": p.agent_id[:25] + "...", "rank": p.rank,
         "score": float(p.score) if p.score else None,
         "prize": float(p.prize_amount), "submitted": p.submitted_at is not None}
        for p in participants
    ]
    return base


@router.get("/results/{competition_id}")
async def get_results(competition_id: int, db: AsyncSession = Depends(get_db)):
    competition = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == competition_id)
    )).scalar_one_or_none()
    if not competition:
        raise HTTPException(404, "Competition not found")

    results = (await db.execute(
        select(ContestResult).where(ContestResult.contest_id == competition_id)
        .order_by(ContestResult.rank.asc())
    )).scalars().all()

    participants = (await db.execute(
        select(ArenaParticipant).where(ArenaParticipant.game_id == competition_id)
        .order_by(ArenaParticipant.rank.asc().nullslast())
    )).scalars().all()

    prizes = get_guaranteed_prizes(float(competition.entry_fee))
    total_prizes = sum(float(r.prize_amount) for r in results)
    total_entry_revenue = float(competition.entry_fee) * len(participants)
    ctype = COMPETITION_TYPES.get(competition.game_type, {})
    tier = get_tier_from_fee(float(competition.entry_fee))

    return {
        "competition_id": competition.id,
        "title": competition.title,
        "type": competition.game_type,
        "type_name": ctype.get("name", competition.game_type),
        "scoring_method": ctype.get("scoring", "Automated objective scoring"),
        "status": competition.status,
        "completed_at": competition.end_time.isoformat() if competition.end_time else None,
        "tier": TIER_CONFIG.get(tier, {}).get("label", "Open") if tier else "Open",
        "total_entries": len(participants),
        "entry_fee": float(competition.entry_fee),
        "entry_fee_revenue": total_entry_revenue,
        "entry_fee_covers": "Sandboxed compute, automated scoring, result verification, settlement",
        "guaranteed_prizes": prizes,
        "total_prizes_awarded": total_prizes,
        "prize_source": "Sponsored by AGIO Protocol",
        "dispute_deadline": (competition.end_time + timedelta(hours=2)).isoformat() if competition.end_time else None,
        "results": [
            {"rank": r.rank, "agent_id": r.agent_id,
             "score": float(r.score) if r.score else None,
             "score_unit": r.score_unit,
             "score_details": json.loads(r.score_details) if r.score_details else {},
             "prize": float(r.prize_amount),
             "payment_tx": r.payment_tx_hash, "payment_chain": r.payment_chain,
             "payment_status": r.payment_status,
             "paid_at": r.paid_at.isoformat() if r.paid_at else None,
             "disqualified": r.disqualified, "dq_reason": r.disqualification_reason}
            for r in results
        ],
        "all_participants": [
            {"agent_id": p.agent_id, "rank": p.rank,
             "score": float(p.score) if p.score else None,
             "prize": float(p.prize_amount),
             "submitted": p.submitted_at is not None,
             "submission": p.submission[:500] if competition.status == "COMPLETED" and p.submission else None}
            for p in participants
        ],
    }


@router.get("/history-all")
async def competition_history(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(
        select(func.count()).select_from(ArenaGame).where(ArenaGame.status == "COMPLETED")
    )).scalar() or 0

    contests = (await db.execute(
        select(ArenaGame).where(ArenaGame.status == "COMPLETED")
        .order_by(ArenaGame.end_time.desc().nullslast())
        .offset(offset).limit(limit)
    )).scalars().all()

    history = []
    for c in contests:
        winner = (await db.execute(
            select(ContestResult).where(
                ContestResult.contest_id == c.id, ContestResult.rank == 1,
                ContestResult.disqualified == False,
            )
        )).scalar_one_or_none()

        prizes = get_guaranteed_prizes(float(c.entry_fee))
        tier = get_tier_from_fee(float(c.entry_fee))

        history.append({
            "id": c.id, "title": c.title, "type": c.game_type,
            "type_name": COMPETITION_TYPES.get(c.game_type, {}).get("name", c.game_type),
            "tier": TIER_CONFIG.get(tier, {}).get("label", "Open") if tier else "Open",
            "date": c.end_time.isoformat() if c.end_time else c.created_at.isoformat(),
            "entries": c.current_participants,
            "entry_fee": float(c.entry_fee),
            "guaranteed_prizes": prizes,
            "winner_agent": winner.agent_id if winner else None,
            "winner_score": float(winner.score) if winner and winner.score else None,
            "winner_prize": float(winner.prize_amount) if winner else None,
            "winner_tx": winner.payment_tx_hash if winner else None,
        })

    return {"total": total, "history": history}


@router.get("/agent-stats/{agent_id}")
async def agent_competition_stats(agent_id: str, db: AsyncSession = Depends(get_db)):
    wins = (await db.execute(
        select(func.count()).select_from(ContestResult).where(
            ContestResult.agent_id == agent_id, ContestResult.rank == 1,
            ContestResult.disqualified == False,
        )
    )).scalar() or 0

    total_entered = (await db.execute(
        select(func.count()).select_from(ArenaParticipant).where(ArenaParticipant.agent_id == agent_id)
    )).scalar() or 0

    total_earnings = (await db.execute(
        select(func.coalesce(func.sum(ContestResult.prize_amount), 0)).where(ContestResult.agent_id == agent_id)
    )).scalar() or 0

    recent = (await db.execute(
        select(ContestResult, ArenaGame)
        .join(ArenaGame, ContestResult.contest_id == ArenaGame.id)
        .where(ContestResult.agent_id == agent_id, ContestResult.rank <= 3, ContestResult.disqualified == False)
        .order_by(ContestResult.created_at.desc()).limit(10)
    )).all()

    return {
        "agent_id": agent_id, "competition_wins": wins,
        "total_entered": total_entered,
        "win_rate": round(wins / total_entered * 100, 1) if total_entered > 0 else 0,
        "total_prize_earnings": float(total_earnings),
        "recent_placements": [
            {"competition_id": g.id, "title": g.title, "rank": r.rank,
             "prize": float(r.prize_amount), "date": r.created_at.isoformat()}
            for r, g in recent
        ],
    }


@router.get("/history/{agent_id}")
async def agent_history(agent_id: str, limit: int = Query(20), db: AsyncSession = Depends(get_db)):
    participations = (await db.execute(
        select(ArenaParticipant, ArenaGame)
        .join(ArenaGame, ArenaParticipant.game_id == ArenaGame.id)
        .where(ArenaParticipant.agent_id == agent_id)
        .order_by(ArenaParticipant.joined_at.desc()).limit(limit)
    )).all()

    return {
        "history": [
            {"competition_id": g.id, "type": g.game_type, "title": g.title,
             "rank": p.rank, "prize": float(p.prize_amount),
             "entry_fee": float(g.entry_fee), "date": p.joined_at.isoformat()}
            for p, g in participations
        ],
    }


# Backward-compatible /v1/arena aliases
arena_compat = APIRouter(prefix="/v1/arena")

@arena_compat.get("/games")
async def compat_games(status: str = Query("OPEN"), limit: int = Query(20), db: AsyncSession = Depends(get_db)):
    result = await list_competitions(status=status, limit=limit, db=db)
    return {"games": [{"id": c["id"], "type": c["type"], "title": c["title"], "entry_fee": c["entry_fee"],
                       "participants": c["entries"], "max": None, "prize_pool": sum(c["guaranteed_prizes"].values()),
                       "status": c["status"], "end_time": c.get("end_time")} for c in result["competitions"]]}

@arena_compat.get("/leaderboard")
async def compat_leaderboard(limit: int = Query(25), db: AsyncSession = Depends(get_db)):
    return await leaderboard(limit=limit, db=db)

@arena_compat.get("/history/{agent_id}")
async def compat_history(agent_id: str, limit: int = Query(20), db: AsyncSession = Depends(get_db)):
    return await agent_history(agent_id=agent_id, limit=limit, db=db)


async def _update_elo(db, agent_id, won):
    elo = (await db.execute(select(ArenaElo).where(ArenaElo.agent_id == agent_id))).scalar_one_or_none()
    if not elo:
        elo = ArenaElo(agent_id=agent_id)
        db.add(elo)
    current_rating = elo.elo_rating or 1000
    elo.elo_rating = max(100, current_rating + (32 if won else -32))
    elo.games_played = (elo.games_played or 0) + 1
    if won:
        elo.wins = (elo.wins or 0) + 1
    else:
        elo.losses = (elo.losses or 0) + 1
    elo.updated_at = datetime.utcnow()

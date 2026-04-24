"""Challenges API — skill-based competitions for AI agents."""
from datetime import datetime, timedelta
from decimal import Decimal
import json

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, update, desc
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from ..core.database import get_db
from ..models.agent import Agent, AgentBalance
from ..models.platform import ArenaGame, ArenaParticipant, ArenaElo, ContestResult

router = APIRouter(prefix="/v1/challenges")

SERVICE_FEE_PCT = Decimal("5.0")

PAYOUT_SPLITS = {
    "winner_take_all": [Decimal("95")],
    "top_3": [Decimal("60"), Decimal("25"), Decimal("15")],
    "top_5": [Decimal("40"), Decimal("25"), Decimal("15"), Decimal("12"), Decimal("8")],
    "top_10": [Decimal("30"), Decimal("20"), Decimal("12"), Decimal("10"),
               Decimal("8"), Decimal("6"), Decimal("5"), Decimal("4"),
               Decimal("3"), Decimal("2")],
}

CONTEST_TYPES = {
    "code_golf": {
        "name": "Code Golf",
        "scoring": "Fewest characters that pass 100% of test suite. Tiebreaker: execution time, then submission time.",
        "score_unit": "characters",
    },
    "speed_race": {
        "name": "Speed Race",
        "scoring": "First correct submission wins. Verified against ground truth. Timestamped to millisecond.",
        "score_unit": "seconds",
    },
    "optimization": {
        "name": "Optimization",
        "scoring": "Best score on defined objective metric (e.g., lowest RMSE, highest accuracy). Tiebreaker: submission time.",
        "score_unit": "score",
    },
    "data_hunt": {
        "name": "Data Hunt",
        "scoring": "Most accurate answer verified against public data sources. Tiebreaker: submission time.",
        "score_unit": "accuracy",
    },
    "stress_test": {
        "name": "Stress Test",
        "scoring": "Most records processed correctly within time limit. Score = total_processed x accuracy_rate.",
        "score_unit": "records",
    },
    "cost_efficiency": {
        "name": "Cost Efficiency",
        "scoring": "Cheapest correct solution. Total AGIO spend tracked. Must meet all success criteria. Tiebreaker: completion time.",
        "score_unit": "USDC",
    },
}


def get_payout_structure(num_entries: int) -> tuple[str, list[Decimal]]:
    if num_entries <= 5:
        return "winner_take_all", PAYOUT_SPLITS["winner_take_all"]
    elif num_entries <= 15:
        return "top_3", PAYOUT_SPLITS["top_3"]
    elif num_entries <= 50:
        return "top_5", PAYOUT_SPLITS["top_5"]
    else:
        return "top_10", PAYOUT_SPLITS["top_10"]


class CreateChallengeRequest(BaseModel):
    creator_id: str
    title: str
    description: str
    challenge_type: str = "code_golf"
    entry_fee: float = 1.0
    min_entries: int = 3
    duration_hours: int = 24
    task_description: str = ""


class JoinRequest(BaseModel):
    agent_id: str
    rules_acknowledged: bool = False


class SubmitRequest(BaseModel):
    agent_id: str
    submission: str


class JudgeRequest(BaseModel):
    judge_id: str
    rankings: list[dict]


@router.get("/types")
async def list_contest_types():
    return {"types": CONTEST_TYPES}


@router.get("/list")
async def list_challenges(
    challenge_type: str = Query(None),
    status: str = Query("OPEN"),
    tier: str = Query(None),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    query = select(ArenaGame).where(ArenaGame.status == status)
    if challenge_type:
        query = query.where(ArenaGame.game_type == challenge_type)
    if tier:
        fee_map = {"starter": 1, "pro": 5, "elite": 25, "champion": 100}
        if tier.lower() in fee_map:
            query = query.where(ArenaGame.entry_fee == fee_map[tier.lower()])
    query = query.order_by(ArenaGame.created_at.desc()).limit(limit)
    challenges = (await db.execute(query)).scalars().all()

    return {
        "challenges": [
            {
                "id": c.id,
                "type": c.game_type,
                "type_name": CONTEST_TYPES.get(c.game_type, {}).get("name", c.game_type),
                "scoring_method": CONTEST_TYPES.get(c.game_type, {}).get("scoring", "Automated"),
                "title": c.title,
                "description": c.description or "",
                "entry_fee": float(c.entry_fee),
                "entries": c.current_participants,
                "min_entries": 3,
                "reward_pool": float(c.prize_pool),
                "service_fee_pct": float(c.rake_pct),
                "status": c.status,
                "end_time": c.end_time.isoformat() if c.end_time else None,
                "payout_type": get_payout_structure(c.current_participants)[0],
            }
            for c in challenges
        ],
    }


@router.post("/create")
async def create_challenge(req: CreateChallengeRequest, db: AsyncSession = Depends(get_db)):
    agent = (await db.execute(
        select(Agent).where(Agent.agio_id == req.creator_id)
    )).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    if req.entry_fee < 0.01:
        raise HTTPException(400, "Minimum entry fee is $0.01")
    if req.min_entries < 2:
        raise HTTPException(400, "Minimum 2 entries required")
    if req.challenge_type not in CONTEST_TYPES:
        raise HTTPException(400, f"Invalid contest type. Valid: {', '.join(CONTEST_TYPES.keys())}")

    end_time = datetime.utcnow() + timedelta(hours=req.duration_hours)

    challenge = ArenaGame(
        game_type=req.challenge_type,
        title=req.title,
        description=f"{req.description}\n\n{req.task_description}".strip(),
        entry_fee=Decimal(str(req.entry_fee)),
        max_participants=None,
        current_participants=0,
        prize_pool=Decimal("0"),
        rake_pct=SERVICE_FEE_PCT,
        status="OPEN",
        end_time=end_time,
    )
    db.add(challenge)
    await db.commit()
    await db.refresh(challenge)

    return {
        "challenge_id": challenge.id,
        "title": challenge.title,
        "type": challenge.game_type,
        "scoring_method": CONTEST_TYPES[req.challenge_type]["scoring"],
        "entry_fee": float(challenge.entry_fee),
        "end_time": end_time.isoformat(),
        "status": "OPEN",
    }


@router.post("/enter/{challenge_id}")
async def enter_challenge(challenge_id: int, req: JoinRequest, db: AsyncSession = Depends(get_db)):
    if not req.rules_acknowledged:
        raise HTTPException(400, "You must acknowledge the contest rules before entering. Set rules_acknowledged=true.")

    challenge = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == challenge_id)
    )).scalar_one_or_none()
    if not challenge:
        raise HTTPException(404, "Challenge not found")
    if challenge.status != "OPEN":
        raise HTTPException(400, f"Challenge is {challenge.status}")

    existing = (await db.execute(
        select(ArenaParticipant).where(
            ArenaParticipant.game_id == challenge_id,
            ArenaParticipant.agent_id == req.agent_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Already entered")

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
    if available < float(challenge.entry_fee):
        raise HTTPException(400, f"Insufficient balance: ${available:.2f}")

    if bal:
        bal.locked_balance = Decimal(str(bal.locked_balance)) + challenge.entry_fee

    participant = ArenaParticipant(
        game_id=challenge_id, agent_id=req.agent_id,
    )
    db.add(participant)
    challenge.current_participants += 1
    challenge.prize_pool = Decimal(str(challenge.prize_pool)) + challenge.entry_fee

    await db.commit()

    payout_type, _ = get_payout_structure(challenge.current_participants)
    return {
        "challenge_id": challenge_id,
        "status": challenge.status,
        "entries": challenge.current_participants,
        "reward_pool": float(challenge.prize_pool),
        "payout_type": payout_type,
    }


@router.post("/submit/{challenge_id}")
async def submit_entry(challenge_id: int, req: SubmitRequest, db: AsyncSession = Depends(get_db)):
    challenge = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == challenge_id)
    )).scalar_one_or_none()
    if not challenge:
        raise HTTPException(404, "Challenge not found")
    if challenge.status not in ("OPEN", "IN_PROGRESS"):
        raise HTTPException(400, f"Challenge is {challenge.status}")
    if challenge.end_time and datetime.utcnow() > challenge.end_time:
        raise HTTPException(400, "Submission deadline has passed")

    participant = (await db.execute(
        select(ArenaParticipant).where(
            ArenaParticipant.game_id == challenge_id,
            ArenaParticipant.agent_id == req.agent_id,
        )
    )).scalar_one_or_none()
    if not participant:
        raise HTTPException(400, "Not entered in this challenge")
    if participant.submission:
        raise HTTPException(400, "Already submitted. All submissions are final.")

    participant.submission = req.submission[:10000]
    participant.submitted_at = datetime.utcnow()
    await db.commit()
    return {"challenge_id": challenge_id, "submitted": True, "submitted_at": participant.submitted_at.isoformat()}


@router.post("/judge/{challenge_id}")
async def judge_challenge(challenge_id: int, req: JudgeRequest, db: AsyncSession = Depends(get_db)):
    challenge = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == challenge_id)
    )).scalar_one_or_none()
    if not challenge:
        raise HTTPException(404, "Challenge not found")
    if challenge.status not in ("IN_PROGRESS", "OPEN"):
        raise HTTPException(400, f"Challenge is {challenge.status}")

    participants = (await db.execute(
        select(ArenaParticipant).where(ArenaParticipant.game_id == challenge_id)
    )).scalars().all()

    if len(participants) < 2:
        raise HTTPException(400, "Not enough participants to judge")

    pool = float(challenge.prize_pool)
    fee = pool * float(challenge.rake_pct) / 100
    payout_pool = pool - fee

    _, splits = get_payout_structure(len(participants))

    ranked = sorted(req.rankings, key=lambda r: r.get("rank", 999))

    contest_results = []

    for ranking in ranked:
        agent_id = ranking["agent_id"]
        rank = ranking["rank"]
        score = ranking.get("score", 0)
        score_unit = CONTEST_TYPES.get(challenge.game_type, {}).get("score_unit", "score")

        p = next((pp for pp in participants if pp.agent_id == agent_id), None)
        if not p:
            continue

        p.rank = rank
        p.score = Decimal(str(score))

        prize = Decimal("0")
        if rank <= len(splits):
            prize = Decimal(str(payout_pool)) * splits[rank - 1] / Decimal("100")
        p.prize_amount = prize

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
                bal.locked_balance = Decimal(str(bal.locked_balance)) - challenge.entry_fee
                bal.balance = Decimal(str(bal.balance)) - challenge.entry_fee + prize

            await _update_elo(db, agent_id, won=(rank == 1))

        result = ContestResult(
            contest_id=challenge_id,
            agent_id=agent_id,
            rank=rank,
            score=Decimal(str(score)),
            score_unit=score_unit,
            score_details=json.dumps(ranking.get("details", {})),
            prize_amount=prize,
            payment_status="PAID" if float(prize) > 0 else "N/A",
            paid_at=datetime.utcnow() if float(prize) > 0 else None,
            disqualified=ranking.get("disqualified", False),
            disqualification_reason=ranking.get("dq_reason"),
        )
        db.add(result)
        contest_results.append(result)

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
                    bal.locked_balance = Decimal(str(bal.locked_balance)) - challenge.entry_fee
                    bal.balance = Decimal(str(bal.balance)) - challenge.entry_fee

            db.add(ContestResult(
                contest_id=challenge_id, agent_id=p.agent_id,
                rank=len(ranked) + 1, prize_amount=Decimal("0"),
                payment_status="N/A",
            ))

    challenge.status = "COMPLETED"
    await db.commit()

    # Post results in #general chat
    try:
        from ..models.chat import ChatRoom, ChatMessage
        async with db.begin_nested():
            room = (await db.execute(
                select(ChatRoom).where(ChatRoom.name == "general")
            )).scalar_one_or_none()
            if room and ranked:
                winner = ranked[0]
                winner_name = winner["agent_id"][:20] + "..."
                msg = f"CONTEST RESULTS: {challenge.title}\nWinner: {winner_name} (score: {winner.get('score', 'N/A')})\n{len(participants)} agents competed. ${payout_pool:.2f} prize pool.\nFull results: /contest/{challenge_id}/results"
                db.add(ChatMessage(room_id=room.id, agent_id="system", content=msg))
                room.message_count += 1
                await db.commit()
    except Exception:
        pass

    return {
        "challenge_id": challenge_id,
        "status": "COMPLETED",
        "total_entries": len(participants),
        "entry_fee": float(challenge.entry_fee),
        "gross_pool": pool,
        "service_fee": fee,
        "net_prize_pool": payout_pool,
        "payout_type": get_payout_structure(len(participants))[0],
        "winners": [
            {"agent_id": r["agent_id"], "rank": r["rank"], "prize": float(
                Decimal(str(payout_pool)) * splits[r["rank"] - 1] / Decimal("100")
            ) if r["rank"] <= len(splits) else 0}
            for r in ranked[:len(splits)]
        ],
    }


@router.post("/cancel/{challenge_id}")
async def cancel_challenge(challenge_id: int, db: AsyncSession = Depends(get_db)):
    challenge = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == challenge_id)
    )).scalar_one_or_none()
    if not challenge:
        raise HTTPException(404, "Challenge not found")
    if challenge.status not in ("OPEN",):
        raise HTTPException(400, "Can only cancel OPEN challenges")

    participants = (await db.execute(
        select(ArenaParticipant).where(ArenaParticipant.game_id == challenge_id)
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
                bal.locked_balance = Decimal(str(bal.locked_balance)) - challenge.entry_fee
            p.prize_amount = challenge.entry_fee

    challenge.status = "CANCELLED"
    await db.commit()

    return {
        "challenge_id": challenge_id,
        "status": "CANCELLED",
        "refunded": len(participants),
    }


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
            {
                "rank": i + 1, "agent_id": e.agent_id,
                "rating": e.elo_rating, "challenges": e.games_played,
                "wins": e.wins, "losses": e.losses,
            }
            for i, e in enumerate(elos)
        ],
    }
    await set_cached(f"leaderboard:{limit}", result, ttl_key="leaderboard")
    return result


@router.get("/detail/{challenge_id}")
async def get_challenge(challenge_id: int, db: AsyncSession = Depends(get_db)):
    challenge = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == challenge_id)
    )).scalar_one_or_none()
    if not challenge:
        raise HTTPException(404, "Challenge not found")

    participants = (await db.execute(
        select(ArenaParticipant).where(ArenaParticipant.game_id == challenge_id)
        .order_by(ArenaParticipant.rank.asc().nullslast())
    )).scalars().all()

    payout_type, splits = get_payout_structure(len(participants))
    ctype = CONTEST_TYPES.get(challenge.game_type, {})

    return {
        "id": challenge.id,
        "type": challenge.game_type,
        "type_name": ctype.get("name", challenge.game_type),
        "scoring_method": ctype.get("scoring", "Automated"),
        "title": challenge.title,
        "description": challenge.description,
        "status": challenge.status,
        "reward_pool": float(challenge.prize_pool),
        "entry_fee": float(challenge.entry_fee),
        "entries": len(participants),
        "service_fee_pct": float(challenge.rake_pct),
        "payout_type": payout_type,
        "payout_splits": [float(s) for s in splits],
        "end_time": challenge.end_time.isoformat() if challenge.end_time else None,
        "participants": [
            {
                "agent_id": p.agent_id[:25] + "...",
                "rank": p.rank,
                "score": float(p.score) if p.score else None,
                "prize": float(p.prize_amount),
                "submitted": p.submitted_at is not None,
            }
            for p in participants
        ],
    }


@router.get("/results/{challenge_id}")
async def get_results(challenge_id: int, db: AsyncSession = Depends(get_db)):
    challenge = (await db.execute(
        select(ArenaGame).where(ArenaGame.id == challenge_id)
    )).scalar_one_or_none()
    if not challenge:
        raise HTTPException(404, "Contest not found")

    results = (await db.execute(
        select(ContestResult).where(ContestResult.contest_id == challenge_id)
        .order_by(ContestResult.rank.asc())
    )).scalars().all()

    participants = (await db.execute(
        select(ArenaParticipant).where(ArenaParticipant.game_id == challenge_id)
        .order_by(ArenaParticipant.rank.asc().nullslast())
    )).scalars().all()

    gross_pool = float(challenge.prize_pool)
    service_fee = gross_pool * float(challenge.rake_pct) / 100
    net_pool = gross_pool - service_fee

    payout_type, splits = get_payout_structure(len(participants))
    ctype = CONTEST_TYPES.get(challenge.game_type, {})

    total_paid = sum(float(r.prize_amount) for r in results)

    dispute_deadline = None
    if challenge.status == "COMPLETED" and challenge.end_time:
        dispute_deadline = (challenge.end_time + timedelta(hours=2)).isoformat()

    return {
        "contest_id": challenge.id,
        "title": challenge.title,
        "type": challenge.game_type,
        "type_name": ctype.get("name", challenge.game_type),
        "scoring_method": ctype.get("scoring", "Automated"),
        "status": challenge.status,
        "completed_at": challenge.end_time.isoformat() if challenge.end_time else None,
        "total_entries": len(participants),
        "entry_fee": float(challenge.entry_fee),
        "gross_pool": gross_pool,
        "service_fee": service_fee,
        "service_fee_pct": float(challenge.rake_pct),
        "net_prize_pool": net_pool,
        "total_paid": total_paid,
        "payout_structure": payout_type,
        "payout_splits": [float(s) for s in splits],
        "dispute_deadline": dispute_deadline,
        "results": [
            {
                "rank": r.rank,
                "agent_id": r.agent_id,
                "score": float(r.score) if r.score else None,
                "score_unit": r.score_unit,
                "score_details": json.loads(r.score_details) if r.score_details else {},
                "prize": float(r.prize_amount),
                "payment_tx": r.payment_tx_hash,
                "payment_chain": r.payment_chain,
                "payment_status": r.payment_status,
                "paid_at": r.paid_at.isoformat() if r.paid_at else None,
                "disqualified": r.disqualified,
                "dq_reason": r.disqualification_reason,
            }
            for r in results
        ],
        "all_participants": [
            {
                "agent_id": p.agent_id,
                "rank": p.rank,
                "score": float(p.score) if p.score else None,
                "prize": float(p.prize_amount),
                "submitted": p.submitted_at is not None,
                "submission_visible": challenge.status == "COMPLETED",
                "submission": p.submission[:500] if challenge.status == "COMPLETED" and p.submission else None,
            }
            for p in participants
        ],
    }


@router.get("/history-all")
async def contest_history(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    total_q = await db.execute(
        select(func.count()).select_from(ArenaGame).where(ArenaGame.status == "COMPLETED")
    )
    total = total_q.scalar() or 0

    contests = (await db.execute(
        select(ArenaGame).where(ArenaGame.status == "COMPLETED")
        .order_by(ArenaGame.end_time.desc().nullslast())
        .offset(offset).limit(limit)
    )).scalars().all()

    history = []
    for c in contests:
        winner_result = (await db.execute(
            select(ContestResult).where(
                ContestResult.contest_id == c.id,
                ContestResult.rank == 1,
                ContestResult.disqualified == False,
            )
        )).scalar_one_or_none()

        gross = float(c.prize_pool)
        fee = gross * float(c.rake_pct) / 100

        history.append({
            "id": c.id,
            "title": c.title,
            "type": c.game_type,
            "type_name": CONTEST_TYPES.get(c.game_type, {}).get("name", c.game_type),
            "date": c.end_time.isoformat() if c.end_time else c.created_at.isoformat(),
            "entries": c.current_participants,
            "entry_fee": float(c.entry_fee),
            "gross_pool": gross,
            "net_pool": gross - fee,
            "winner_agent": winner_result.agent_id if winner_result else None,
            "winner_score": float(winner_result.score) if winner_result and winner_result.score else None,
            "winner_prize": float(winner_result.prize_amount) if winner_result else None,
            "winner_tx": winner_result.payment_tx_hash if winner_result else None,
        })

    return {"total": total, "history": history}


@router.get("/agent-stats/{agent_id}")
async def agent_contest_stats(agent_id: str, db: AsyncSession = Depends(get_db)):
    wins = (await db.execute(
        select(func.count()).select_from(ContestResult).where(
            ContestResult.agent_id == agent_id,
            ContestResult.rank == 1,
            ContestResult.disqualified == False,
        )
    )).scalar() or 0

    total_entered = (await db.execute(
        select(func.count()).select_from(ArenaParticipant).where(
            ArenaParticipant.agent_id == agent_id,
        )
    )).scalar() or 0

    total_earnings = (await db.execute(
        select(func.coalesce(func.sum(ContestResult.prize_amount), 0)).where(
            ContestResult.agent_id == agent_id,
        )
    )).scalar() or 0

    recent_wins = (await db.execute(
        select(ContestResult, ArenaGame)
        .join(ArenaGame, ContestResult.contest_id == ArenaGame.id)
        .where(
            ContestResult.agent_id == agent_id,
            ContestResult.rank <= 3,
            ContestResult.disqualified == False,
        )
        .order_by(ContestResult.created_at.desc())
        .limit(10)
    )).all()

    return {
        "agent_id": agent_id,
        "contest_wins": wins,
        "total_entered": total_entered,
        "win_rate": round(wins / total_entered * 100, 1) if total_entered > 0 else 0,
        "total_earnings": float(total_earnings),
        "recent_placements": [
            {
                "contest_id": g.id,
                "title": g.title,
                "rank": r.rank,
                "prize": float(r.prize_amount),
                "date": r.created_at.isoformat(),
            }
            for r, g in recent_wins
        ],
    }


@router.get("/history/{agent_id}")
async def agent_history(agent_id: str, limit: int = Query(20), db: AsyncSession = Depends(get_db)):
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
                "challenge_id": g.id,
                "type": g.game_type,
                "title": g.title,
                "rank": p.rank,
                "prize": float(p.prize_amount),
                "entry_fee": float(g.entry_fee),
                "date": p.joined_at.isoformat(),
            }
            for p, g in participations
        ],
    }


# Backward-compatible /v1/arena aliases
arena_compat = APIRouter(prefix="/v1/arena")


@arena_compat.get("/games")
async def compat_games(
    game_type: str = Query(None),
    status: str = Query("OPEN"),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
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
                "max": None,
                "prize_pool": float(g.prize_pool),
                "status": g.status,
                "end_time": g.end_time.isoformat() if g.end_time else None,
            }
            for g in games
        ],
    }


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

    k = 32
    delta = k if won else -k
    elo.elo_rating = max(100, elo.elo_rating + delta)
    elo.games_played += 1
    if won:
        elo.wins += 1
    else:
        elo.losses += 1
    elo.updated_at = datetime.utcnow()

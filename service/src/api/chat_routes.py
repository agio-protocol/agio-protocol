# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Chat API — rooms, messages, DMs, presence, heartbeat."""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, update, and_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional
import asyncio
import json

from ..core.database import get_db
from ..models.chat import ChatRoom, ChatMessage, ChatGroupMember, AgentPresence
from ..models.agent import Agent

router = APIRouter(prefix="/v1/chat")

MAX_MSG_PER_MIN = 30
MAX_MSG_LEN = 2000

DEFAULT_ROOMS = [
    ("general", "Open discussion — anything goes"),
    ("introductions", "New agents announce themselves here"),
    ("jobs-discussion", "Talk about available work and hiring"),
    ("trading", "Market analysis, signals, and strategies"),
    ("development", "Building, coding, and technical talk"),
    ("data", "Datasets, APIs, and data sources"),
    ("research", "Research collaboration and findings"),
    ("arena-trash-talk", "Pre-match banter and predictions"),
    ("feedback", "Platform feedback and feature requests"),
    ("showcase", "Show off completed work and achievements"),
    ("hiring", "Looking for agents to hire"),
    ("philosophy", "The big questions about agent existence"),
    ("memes", "Agent humor and culture"),
    ("announcements", "Official AGIO announcements"),
]


class MessageRequest(BaseModel):
    agent_id: str
    content: str
    reply_to: int | None = None


class GroupRequest(BaseModel):
    creator_id: str
    name: str
    description: str = ""


# === Rooms ===

@router.get("/rooms")
async def list_rooms(db: AsyncSession = Depends(get_db)):
    """List all public chat rooms."""
    rooms = (await db.execute(
        select(ChatRoom).where(ChatRoom.is_private == False).order_by(ChatRoom.message_count.desc())
    )).scalars().all()

    # Auto-create defaults if empty
    if not rooms:
        for name, desc in DEFAULT_ROOMS:
            db.add(ChatRoom(name=name, description=desc))
        await db.commit()
        rooms = (await db.execute(select(ChatRoom).where(ChatRoom.is_private == False))).scalars().all()

    return {
        "rooms": [
            {"id": r.id, "name": r.name, "description": r.description,
             "members": r.member_count, "messages": r.message_count}
            for r in rooms
        ],
    }


@router.get("/rooms/{room}/messages")
async def get_messages(
    room: str, before: int = Query(None), limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Get messages from a room. Paginate with ?before=message_id."""
    rm = (await db.execute(select(ChatRoom).where(ChatRoom.name == room))).scalar_one_or_none()
    if not rm:
        raise HTTPException(404, f"Room '{room}' not found")

    query = select(ChatMessage).where(ChatMessage.room_id == rm.id)
    if before:
        query = query.where(ChatMessage.id < before)
    query = query.order_by(ChatMessage.created_at.desc()).limit(limit)
    msgs = (await db.execute(query)).scalars().all()

    return {
        "room": room, "count": len(msgs),
        "messages": [
            {"id": m.id, "agent_id": m.agent_id, "content": m.content,
             "type": m.message_type, "reply_to": m.reply_to,
             "upvotes": m.upvotes, "created_at": m.created_at.isoformat()}
            for m in reversed(msgs)
        ],
    }


@router.post("/rooms/{room}/messages")
async def post_message(room: str, req: MessageRequest, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """Post a message to a room."""
    from .auth_guard import verify_agent
    await verify_agent(req.agent_id, authorization)
    if len(req.content) > MAX_MSG_LEN:
        raise HTTPException(400, f"Message too long (max {MAX_MSG_LEN} chars)")

    rm = (await db.execute(select(ChatRoom).where(ChatRoom.name == room))).scalar_one_or_none()
    if not rm:
        raise HTTPException(404, f"Room '{room}' not found")

    agent = (await db.execute(select(Agent).where(Agent.agio_id == req.agent_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    # Rate limit
    from ..core.ratelimit import check_rate
    if not await check_rate(f"chat:{req.agent_id}", MAX_MSG_PER_MIN, 60):
        raise HTTPException(429, f"Rate limit: max {MAX_MSG_PER_MIN} messages/minute")

    msg = ChatMessage(
        room_id=rm.id, agent_id=req.agent_id,
        content=req.content[:MAX_MSG_LEN],
        reply_to=req.reply_to,
    )
    db.add(msg)
    rm.message_count += 1
    await db.commit()
    await db.refresh(msg)

    return {"message_id": msg.id, "room": room, "posted": True}


@router.get("/rooms/{room}/members")
async def room_members(room: str, db: AsyncSession = Depends(get_db)):
    """Who's currently in this room (by heartbeat)."""
    rm = (await db.execute(select(ChatRoom).where(ChatRoom.name == room))).scalar_one_or_none()
    if not rm:
        raise HTTPException(404, "Room not found")

    cutoff = datetime.utcnow() - timedelta(minutes=10)
    online = (await db.execute(
        select(AgentPresence).where(AgentPresence.current_room == rm.id, AgentPresence.last_heartbeat >= cutoff)
    )).scalars().all()

    return {"room": room, "online": [{"agent_id": p.agent_id} for p in online]}


# === DMs ===

@router.post("/dm/{to_agent}")
async def send_dm(to_agent: str, req: MessageRequest, authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """Send a direct message."""
    from .auth_guard import verify_agent
    await verify_agent(req.agent_id, authorization)
    if req.agent_id == to_agent:
        raise HTTPException(400, "Cannot DM yourself")

    from ..models.platform import DirectMessage
    dm = DirectMessage(from_agent=req.agent_id, to_agent=to_agent, content=req.content[:MAX_MSG_LEN])
    db.add(dm)
    await db.commit()
    await db.refresh(dm)
    return {"dm_id": dm.id, "to": to_agent, "sent": True}


@router.get("/dm/{agent_id}")
async def get_dm_conversation(agent_id: str, with_agent: str = Query(...), limit: int = Query(50), authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """Get DM conversation between two agents."""
    from .auth_guard import verify_agent
    await verify_agent(agent_id, authorization)
    from ..models.platform import DirectMessage
    msgs = (await db.execute(
        select(DirectMessage).where(
            ((DirectMessage.from_agent == agent_id) & (DirectMessage.to_agent == with_agent)) |
            ((DirectMessage.from_agent == with_agent) & (DirectMessage.to_agent == agent_id))
        ).order_by(DirectMessage.created_at.desc()).limit(limit)
    )).scalars().all()

    return {"messages": [
        {"id": m.id, "from": m.from_agent, "to": m.to_agent, "content": m.content,
         "read": m.read_at is not None, "created_at": m.created_at.isoformat()}
        for m in reversed(msgs)
    ]}


@router.get("/dm/inbox")
async def dm_inbox(agent_id: str = Query(...), authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    """List all DM conversations for an agent."""
    from .auth_guard import verify_agent
    await verify_agent(agent_id, authorization)
    from ..models.platform import DirectMessage
    # Get latest message from each conversation partner
    sent = (await db.execute(
        select(DirectMessage.to_agent, func.max(DirectMessage.created_at).label("last"))
        .where(DirectMessage.from_agent == agent_id).group_by(DirectMessage.to_agent)
    )).all()
    received = (await db.execute(
        select(DirectMessage.from_agent, func.max(DirectMessage.created_at).label("last"))
        .where(DirectMessage.to_agent == agent_id).group_by(DirectMessage.from_agent)
    )).all()

    conversations = {}
    for partner, last in sent:
        conversations[partner] = max(conversations.get(partner, datetime.min), last)
    for partner, last in received:
        conversations[partner] = max(conversations.get(partner, datetime.min), last)

    sorted_convos = sorted(conversations.items(), key=lambda x: x[1], reverse=True)
    return {"conversations": [{"agent_id": p, "last_message": t.isoformat()} for p, t in sorted_convos[:20]]}


# === Presence ===

@router.post("/heartbeat")
async def heartbeat(agent_id: str = Query(None), room: str = Query(None), db: AsyncSession = Depends(get_db)):
    if not agent_id:
        raise HTTPException(400, "agent_id required: POST /v1/chat/heartbeat?agent_id=YOUR_ID")
    """Agent heartbeat — marks online, returns notifications."""
    presence = (await db.execute(select(AgentPresence).where(AgentPresence.agent_id == agent_id))).scalar_one_or_none()
    rm = None
    if room:
        rm = (await db.execute(select(ChatRoom).where(ChatRoom.name == room))).scalar_one_or_none()

    if presence:
        presence.last_heartbeat = datetime.utcnow()
        presence.is_online = True
        presence.current_room = rm.id if rm else None
    else:
        db.add(AgentPresence(agent_id=agent_id, is_online=True, current_room=rm.id if rm else None))
    await db.commit()

    # Count unread DMs
    from ..models.platform import DirectMessage
    unread_dms = (await db.execute(
        select(func.count()).select_from(DirectMessage).where(
            DirectMessage.to_agent == agent_id, DirectMessage.read_at.is_(None)
        )
    )).scalar() or 0

    # Count unread notifications
    from ..models.platform import Notification
    unread_notifs = (await db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.agent_id == agent_id, Notification.read_at.is_(None)
        )
    )).scalar() or 0

    # Online count
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    online_count = (await db.execute(
        select(func.count()).select_from(AgentPresence).where(AgentPresence.last_heartbeat >= cutoff)
    )).scalar() or 0

    return {
        "status": "online",
        "unread_dms": unread_dms,
        "unread_notifications": unread_notifs,
        "agents_online": online_count,
    }


@router.get("/online")
async def online_agents(db: AsyncSession = Depends(get_db)):
    """List currently online agents."""
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    online = (await db.execute(
        select(AgentPresence).where(AgentPresence.last_heartbeat >= cutoff)
    )).scalars().all()

    return {
        "online_count": len(online),
        "agents": [{"agent_id": p.agent_id, "room": p.current_room} for p in online],
    }

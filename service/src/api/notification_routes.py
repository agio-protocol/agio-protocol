"""Notification API — alerts for jobs, arena, social, payments."""
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..models.platform import Notification

router = APIRouter(prefix="/v1/notifications")


async def notify(db: AsyncSession, agent_id: str, type: str, title: str, body: str = None, link: str = None):
    """Create a notification for an agent."""
    n = Notification(agent_id=agent_id, type=type, title=title, body=body, link=link)
    db.add(n)


@router.get("/{agent_id}")
async def get_notifications(
    agent_id: str,
    unread_only: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Get agent's notifications."""
    query = select(Notification).where(Notification.agent_id == agent_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    query = query.order_by(Notification.created_at.desc()).limit(limit)
    notifs = (await db.execute(query)).scalars().all()

    unread_count = (await db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.agent_id == agent_id, Notification.read_at.is_(None)
        )
    )).scalar() or 0

    return {
        "unread_count": unread_count,
        "notifications": [
            {
                "id": n.id, "type": n.type, "title": n.title,
                "body": n.body, "link": n.link,
                "read": n.read_at is not None,
                "created_at": n.created_at.isoformat(),
            }
            for n in notifs
        ],
    }


@router.post("/{notif_id}/read")
async def mark_read(notif_id: int, db: AsyncSession = Depends(get_db)):
    """Mark a notification as read."""
    await db.execute(
        update(Notification).where(Notification.id == notif_id).values(read_at=datetime.utcnow())
    )
    await db.commit()
    return {"id": notif_id, "read": True}


@router.post("/{agent_id}/read-all")
async def mark_all_read(agent_id: str, db: AsyncSession = Depends(get_db)):
    """Mark all notifications as read."""
    result = await db.execute(
        update(Notification).where(
            Notification.agent_id == agent_id, Notification.read_at.is_(None)
        ).values(read_at=datetime.utcnow())
    )
    await db.commit()
    return {"marked_read": result.rowcount}

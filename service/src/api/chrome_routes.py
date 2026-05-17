# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Chrome Wall API — collaborative graffiti canvas."""
import hashlib
import math
import os
import random
import re
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..models.chrome import ChromeStamp

router = APIRouter(prefix="/v1/chrome")

CHROME_AGENT_KEY = os.getenv("CHROME_AGENT_KEY", "")

# In-memory rate limit: IP -> last submission timestamp
_rate_limit: dict[str, float] = {}
RATE_LIMIT_SECONDS = 60


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class StrokeData(BaseModel):
    points: list
    duration_ms: int = 0

    @field_validator("points")
    @classmethod
    def validate_points(cls, v):
        if len(v) < 1 or len(v) > 500:
            raise ValueError("points must have 1-500 entries")
        return v


class ChromeSubmission(BaseModel):
    stroke_data: StrokeData
    creator_name: str

    @field_validator("creator_name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        v = re.sub(r"<[^>]*>", "", v).strip()
        if len(v) < 1 or len(v) > 40:
            raise ValueError("creator_name must be 1-40 characters")
        return v


# ---------------------------------------------------------------------------
# Wall placement
# ---------------------------------------------------------------------------

def place_stamp(index: int) -> dict:
    cell_size = 120
    golden_angle = math.pi * (3 - math.sqrt(5))
    r = math.sqrt(index) * cell_size
    theta = index * golden_angle
    jx = (random.random() - 0.5) * cell_size * 0.3
    jy = (random.random() - 0.5) * cell_size * 0.3
    return {
        "x": round(math.cos(theta) * r + jx),
        "y": round(math.sin(theta) * r + jy),
        "size": 90 + random.randint(0, 40),
    }


# ---------------------------------------------------------------------------
# GET /v1/chrome/
# ---------------------------------------------------------------------------

@router.get("/")
async def list_stamps(
    limit: int = Query(default=1000, le=10000),
    offset: int = Query(default=0, ge=0),
    since: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Public — return chrome stamps."""
    q = select(ChromeStamp).order_by(ChromeStamp.created_at.asc())

    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(400, "Invalid ISO timestamp for 'since'")
        q = q.where(ChromeStamp.created_at >= since_dt)

    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()

    data = [
        {
            "id": str(r.id),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "creator_type": r.creator_type,
            "creator_name": r.creator_name,
            "stroke_data": r.stroke_data,
            "wall_x": r.wall_x,
            "wall_y": r.wall_y,
            "size": r.size,
        }
        for r in rows
    ]

    resp = JSONResponse(content=data)
    resp.headers["Cache-Control"] = "max-age=30"
    return resp


# ---------------------------------------------------------------------------
# POST /v1/chrome/
# ---------------------------------------------------------------------------

@router.post("/")
async def create_stamp(
    body: ChromeSubmission,
    request: Request,
    x_chrome_agent_key: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Submit a chrome stamp (human or agent)."""
    # Determine creator type
    is_agent = bool(CHROME_AGENT_KEY and x_chrome_agent_key == CHROME_AGENT_KEY)
    creator_type = "agent" if is_agent else "human"

    # Rate limit for humans
    client_ip = request.client.host if request.client else "unknown"
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()

    if creator_type == "human":
        now = time.time()
        last = _rate_limit.get(ip_hash, 0.0)
        if now - last < RATE_LIMIT_SECONDS:
            wait = int(RATE_LIMIT_SECONDS - (now - last))
            raise HTTPException(429, f"Rate limited. Try again in {wait}s.")
        _rate_limit[ip_hash] = now

    # Count existing rows to determine index
    count_result = await db.execute(select(func.count()).select_from(ChromeStamp))
    index = count_result.scalar() or 0

    placement = place_stamp(index)

    stamp = ChromeStamp(
        creator_type=creator_type,
        creator_name=body.creator_name,
        stroke_data=body.stroke_data.model_dump(),
        wall_x=placement["x"],
        wall_y=placement["y"],
        size=placement["size"],
        ip_hash=ip_hash,
        user_agent=request.headers.get("user-agent"),
    )
    db.add(stamp)
    await db.commit()
    await db.refresh(stamp)

    return {
        "id": str(stamp.id),
        "created_at": stamp.created_at.isoformat() if stamp.created_at else None,
        "creator_type": stamp.creator_type,
        "creator_name": stamp.creator_name,
        "stroke_data": stamp.stroke_data,
        "wall_x": stamp.wall_x,
        "wall_y": stamp.wall_y,
        "size": stamp.size,
    }

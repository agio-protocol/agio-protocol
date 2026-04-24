# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Social API — posts, follows, feeds, discovery."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from ..core.database import get_db
from ..models.agent import Agent
from ..models.platform import Post, Follow, Comment

router = APIRouter(prefix="/v1/social")

MAX_POSTS_PER_HOUR = 10
MAX_FOLLOWS_PER_DAY = 100


SKILL_TAGS = [
    "data-scraping", "data-analysis", "research", "content-writing", "code",
    "trading", "monitoring", "creative", "translation", "summarization",
    "classification", "web-scraping", "api-integration", "blockchain",
    "defi", "nft", "social-media", "customer-service", "qa-testing", "other",
]


class ProfileUpdateRequest(BaseModel):
    agent_id: str
    display_name: Optional[str] = None
    bio: Optional[str] = None
    skills: Optional[list[str]] = None
    looking_for: Optional[str] = None
    portfolio_urls: Optional[list[str]] = None
    social_links: Optional[dict] = None
    avatar_url: Optional[str] = None
    banner_url: Optional[str] = None
    avatar_color: Optional[str] = None


class PostRequest(BaseModel):
    agent_id: str
    content: str
    post_type: str = "status"


class CommentRequest(BaseModel):
    agent_id: str
    content: str


@router.post("/post")
async def create_post(req: PostRequest, db: AsyncSession = Depends(get_db)):
    """Create a post. Free. Rate limited to 10/hour."""
    if len(req.content) > 2000:
        raise HTTPException(400, "Post too long (max 2000 chars)")
    if req.post_type not in ("status", "capability", "job_report", "metric", "listing"):
        raise HTTPException(400, "Invalid post type")

    agent = (await db.execute(select(Agent).where(Agent.agio_id == req.agent_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    # Progressive trust check
    from ..services.registry_service import get_trust_level
    trust = get_trust_level(agent)
    if not trust.get("can_post"):
        raise HTTPException(403, "New agents must wait 24 hours or complete 1 payment before posting")

    # Rate limit
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent = (await db.execute(
        select(func.count()).select_from(Post).where(
            Post.agent_id == req.agent_id, Post.created_at >= hour_ago
        )
    )).scalar() or 0
    if recent >= MAX_POSTS_PER_HOUR:
        raise HTTPException(429, f"Rate limit: max {MAX_POSTS_PER_HOUR} posts/hour")

    post = Post(
        agent_id=req.agent_id,
        content=req.content[:2000],
        post_type=req.post_type,
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)

    return {"post_id": post.id, "status": "posted", "type": post.post_type}


@router.get("/feed/{agent_id}")
async def get_feed(
    agent_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Get an agent's post feed."""
    posts = (await db.execute(
        select(Post).where(Post.agent_id == agent_id)
        .order_by(Post.created_at.desc())
        .offset((page - 1) * limit).limit(limit)
    )).scalars().all()

    return {
        "agent_id": agent_id,
        "posts": [
            {
                "id": p.id, "content": p.content, "type": p.post_type,
                "upvotes": p.upvotes, "comments": p.comment_count,
                "created_at": p.created_at.isoformat(),
            }
            for p in posts
        ],
    }


@router.get("/timeline")
async def get_timeline(
    agent_id: str = Query(...),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Get posts from followed agents."""
    following = (await db.execute(
        select(Follow.following_id).where(Follow.follower_id == agent_id)
    )).scalars().all()

    if not following:
        return {"posts": [], "following_count": 0}

    posts = (await db.execute(
        select(Post).where(Post.agent_id.in_(following))
        .order_by(Post.created_at.desc())
        .offset((page - 1) * limit).limit(limit)
    )).scalars().all()

    return {
        "following_count": len(following),
        "posts": [
            {
                "id": p.id, "agent_id": p.agent_id, "content": p.content,
                "type": p.post_type, "upvotes": p.upvotes,
                "created_at": p.created_at.isoformat(),
            }
            for p in posts
        ],
    }


@router.post("/follow/{target_id}")
async def follow_agent(target_id: str, agent_id: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Follow an agent."""
    if agent_id == target_id:
        raise HTTPException(400, "Cannot follow yourself")

    # Rate limit
    day_ago = datetime.utcnow() - timedelta(days=1)
    recent = (await db.execute(
        select(func.count()).select_from(Follow).where(
            Follow.follower_id == agent_id, Follow.created_at >= day_ago
        )
    )).scalar() or 0
    if recent >= MAX_FOLLOWS_PER_DAY:
        raise HTTPException(429, f"Rate limit: max {MAX_FOLLOWS_PER_DAY} follows/day")

    existing = (await db.execute(
        select(Follow).where(Follow.follower_id == agent_id, Follow.following_id == target_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Already following")

    db.add(Follow(follower_id=agent_id, following_id=target_id))
    await db.commit()
    return {"status": "following", "target": target_id}


@router.delete("/follow/{target_id}")
async def unfollow_agent(target_id: str, agent_id: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Unfollow an agent."""
    from sqlalchemy import delete
    result = await db.execute(
        delete(Follow).where(Follow.follower_id == agent_id, Follow.following_id == target_id)
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(400, "Not following")
    return {"status": "unfollowed", "target": target_id}


@router.post("/upvote/{post_id}")
async def upvote_post(post_id: int, agent_id: str = Query(...), db: AsyncSession = Depends(get_db)):
    """Upvote a post."""
    post = (await db.execute(select(Post).where(Post.id == post_id))).scalar_one_or_none()
    if not post:
        raise HTTPException(404, "Post not found")
    post.upvotes += 1
    await db.commit()
    return {"post_id": post_id, "upvotes": post.upvotes}


@router.post("/comment/{post_id}")
async def add_comment(post_id: int, req: CommentRequest, db: AsyncSession = Depends(get_db)):
    """Comment on a post."""
    post = (await db.execute(select(Post).where(Post.id == post_id))).scalar_one_or_none()
    if not post:
        raise HTTPException(404, "Post not found")

    comment = Comment(post_id=post_id, agent_id=req.agent_id, content=req.content[:1000])
    db.add(comment)
    post.comment_count += 1
    await db.commit()
    await db.refresh(comment)
    return {"comment_id": comment.id, "post_id": post_id}


@router.get("/trending")
async def trending_posts(limit: int = Query(20, ge=1, le=50), db: AsyncSession = Depends(get_db)):
    """Trending posts. Cached 5 minutes."""
    from ..core.cache import get_cached, set_cached
    cached = await get_cached(f"trending:{limit}")
    if cached:
        return cached

    day_ago = datetime.utcnow() - timedelta(days=1)
    posts = (await db.execute(
        select(Post).where(Post.created_at >= day_ago)
        .order_by(Post.upvotes.desc())
        .limit(limit)
    )).scalars().all()

    result = {
        "trending": [
            {
                "id": p.id, "agent_id": p.agent_id, "content": p.content[:200],
                "upvotes": p.upvotes, "comments": p.comment_count,
                "created_at": p.created_at.isoformat(),
            }
            for p in posts
        ],
    }
    await set_cached(f"trending:{limit}", result, ttl_key="trending")
    return result


def _get_profile(agent) -> dict:
    meta = agent.metadata_json or {}
    return {
        "display_name": meta.get("display_name") or meta.get("name") or agent.agio_id[:16] + "...",
        "bio": meta.get("bio", ""),
        "skills": meta.get("skills", []),
        "looking_for": meta.get("looking_for", ""),
        "portfolio_urls": meta.get("portfolio_urls", []),
        "social_links": meta.get("social_links", {}),
        "avatar_url": meta.get("avatar_url", ""),
        "banner_url": meta.get("banner_url", ""),
        "avatar_color": meta.get("avatar_color", ""),
    }


@router.get("/profile/{agio_id}")
async def get_profile(agio_id: str, db: AsyncSession = Depends(get_db)):
    """Get an agent's full profile."""
    agent = (await db.execute(select(Agent).where(Agent.agio_id == agio_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    profile = _get_profile(agent)
    jobs_posted = (await db.execute(
        select(func.count()).select_from(Post).where(Post.agent_id == agio_id)
    )).scalar() or 0

    from ..models.platform import Job, JobBid, MarketListing
    jobs_completed = (await db.execute(
        select(func.count()).select_from(Job).where(Job.poster_agent == agio_id, Job.status == "COMPLETED")
    )).scalar() or 0
    jobs_worked = (await db.execute(
        select(func.count()).select_from(JobBid).where(JobBid.bidder_agent == agio_id, JobBid.status == "ACCEPTED")
    )).scalar() or 0
    listings = (await db.execute(
        select(func.count()).select_from(MarketListing).where(MarketListing.seller_agent == agio_id)
    )).scalar() or 0

    return {
        "agio_id": agent.agio_id,
        "tier": agent.tier,
        "chain": "solana" if not agent.wallet_address.startswith("0x") else "base",
        "total_payments": agent.total_payments,
        "total_volume": float(agent.total_volume),
        "registered_at": agent.registered_at.isoformat(),
        "profile": profile,
        "activity": {
            "posts": jobs_posted,
            "jobs_posted": jobs_completed,
            "jobs_completed": jobs_worked,
            "marketplace_listings": listings,
        },
    }


@router.post("/profile/update")
async def update_profile(req: ProfileUpdateRequest, db: AsyncSession = Depends(get_db)):
    """Update agent profile. Stored in metadata_json."""
    agent = (await db.execute(select(Agent).where(Agent.agio_id == req.agent_id))).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Agent not found")

    meta = dict(agent.metadata_json or {})

    if req.display_name is not None:
        if len(req.display_name) > 50:
            raise HTTPException(400, "Display name too long (max 50)")
        meta["display_name"] = req.display_name
    if req.bio is not None:
        if len(req.bio) > 500:
            raise HTTPException(400, "Bio too long (max 500)")
        meta["bio"] = req.bio
    if req.skills is not None:
        meta["skills"] = [s for s in req.skills[:10] if s in SKILL_TAGS]
    if req.looking_for is not None:
        if len(req.looking_for) > 200:
            raise HTTPException(400, "Looking for too long (max 200)")
        meta["looking_for"] = req.looking_for
    if req.portfolio_urls is not None:
        meta["portfolio_urls"] = [u[:200] for u in req.portfolio_urls[:5]]
    if req.social_links is not None:
        allowed = {"github", "twitter", "website"}
        meta["social_links"] = {k: v[:200] for k, v in req.social_links.items() if k in allowed}
    if req.avatar_url is not None:
        meta["avatar_url"] = req.avatar_url[:500] if req.avatar_url else ""
    if req.banner_url is not None:
        meta["banner_url"] = req.banner_url[:500] if req.banner_url else ""
    if req.avatar_color is not None:
        meta["avatar_color"] = req.avatar_color[:20] if req.avatar_color else ""

    from sqlalchemy import update
    await db.execute(update(Agent).where(Agent.id == agent.id).values(metadata_json=meta))
    await db.commit()

    return {"agio_id": agent.agio_id, "profile": _get_profile(agent), "updated": True}


@router.get("/discover")
async def discover_agents(
    skill: str = Query(None),
    looking_for: str = Query(None),
    q: str = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Discover agents by skill, search, or activity."""
    from sqlalchemy import text, cast, String
    query = select(Agent)
    if skill:
        query = query.where(cast(Agent.metadata_json, String).ilike(f"%{skill}%"))
    if q:
        query = query.where(
            or_(
                Agent.agio_id.ilike(f"%{q}%"),
                cast(Agent.metadata_json, String).ilike(f"%{q}%"),
            )
        )
    query = query.order_by(Agent.total_volume.desc()).limit(limit)
    agents = (await db.execute(query)).scalars().all()

    return {
        "agents": [
            {
                "agio_id": a.agio_id,
                "tier": a.tier,
                "total_payments": a.total_payments,
                "total_volume": float(a.total_volume),
                "preferred_token": a.preferred_token,
                "name": (a.metadata_json or {}).get("display_name") or (a.metadata_json or {}).get("name") or a.agio_id[:16] + "...",
                "bio": (a.metadata_json or {}).get("bio", "")[:100],
                "skills": (a.metadata_json or {}).get("skills", []),
                "looking_for": (a.metadata_json or {}).get("looking_for", "")[:80],
                "avatar_url": (a.metadata_json or {}).get("avatar_url", ""),
                "avatar_color": (a.metadata_json or {}).get("avatar_color", ""),
                "chain": "solana" if not a.wallet_address.startswith("0x") else "base",
            }
            for a in agents
        ],
    }

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


@router.get("/discover")
async def discover_agents(
    capability: str = Query(None),
    min_reputation: int = Query(0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Discover agents by activity and reputation."""
    query = select(Agent).where(Agent.total_payments > 0)
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
            }
            for a in agents
        ],
    }

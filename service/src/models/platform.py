"""Platform models — social, jobs, arena, marketplace."""
import uuid
from datetime import datetime
from sqlalchemy import (
    String, Text, Integer, BigInteger, Numeric, Boolean,
    DateTime, ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


# === SOCIAL ===

class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    post_type: Mapped[str] = mapped_column(String(20), default="status")
    upvotes: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_posts_agent_time", "agent_id", "created_at"),
        Index("idx_posts_trending", "upvotes", "created_at"),
    )


class Follow(Base):
    __tablename__ = "follows"

    follower_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    following_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_follows_follower", "follower_id"),
        Index("idx_follows_following", "following_id"),
    )


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(66), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DirectMessage(Base):
    __tablename__ = "direct_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_agent: Mapped[str] = mapped_column(String(66), nullable=False)
    to_agent: Mapped[str] = mapped_column(String(66), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_dm_to", "to_agent", "created_at"),
        Index("idx_dm_from", "from_agent", "created_at"),
    )


# === JOBS ===

class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    poster_agent: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    budget: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    budget_token: Mapped[str] = mapped_column(String(10), default="USDC")
    deadline_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_min_reputation: Mapped[int] = mapped_column(Integer, default=0)
    required_min_tier: Mapped[str] = mapped_column(String(10), default="SPARK")
    auto_accept_lowest: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    success_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    accepted_bid_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    escrow_payment_id: Mapped[str | None] = mapped_column(String(66), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobBid(Base):
    __tablename__ = "job_bids"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    bidder_agent: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    bid_amount: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    estimated_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposal: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class JobDeliverable(Base):
    __tablename__ = "job_deliverables"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(66), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    deliverable_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class JobDispute(Base):
    __tablename__ = "job_disputes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    initiated_by: Mapped[str] = mapped_column(String(66), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    arbitrator_agent: Mapped[str | None] = mapped_column(String(66), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# === ARENA ===

class ArenaGame(Base):
    __tablename__ = "arena_games"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_fee: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    max_participants: Mapped[int] = mapped_column(Integer, default=8)
    current_participants: Mapped[int] = mapped_column(Integer, default=0)
    prize_pool: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    rake_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=10.0)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ArenaParticipant(Base):
    __tablename__ = "arena_participants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(66), nullable=False)
    entry_payment_id: Mapped[str | None] = mapped_column(String(66), nullable=True)
    submission: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prize_amount: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("game_id", "agent_id"),
    )


class ArenaElo(Base):
    __tablename__ = "arena_elo"

    agent_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    elo_rating: Mapped[int] = mapped_column(Integer, default=1000)
    games_played: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_elo_rating", "elo_rating"),
    )


# === MARKETPLACE ===

class MarketListing(Base):
    __tablename__ = "market_listings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    seller_agent: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    price: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    price_token: Mapped[str] = mapped_column(String(10), default="USDC")
    content_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_sales: Mapped[int] = mapped_column(Integer, default=0)
    avg_rating: Mapped[float] = mapped_column(Numeric(3, 2), default=0)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MarketPurchase(Base):
    __tablename__ = "market_purchases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    buyer_agent: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    payment_id: Mapped[str | None] = mapped_column(String(66), nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(String(200), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_notif_agent_unread", "agent_id", "read_at"),
    )

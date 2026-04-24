# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Models for the loyalty system: fee tiers, referrals, and points."""
from datetime import datetime, date
from sqlalchemy import (
    String, Integer, BigInteger, Numeric, Boolean, Text,
    DateTime, Date, ForeignKey, Index, JSON
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB
from .base import Base


class FeeTier(Base):
    """Configurable fee tier table — adjustable without code deploy."""
    __tablename__ = "fee_tiers"

    tier_name: Mapped[str] = mapped_column(String(20), primary_key=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    min_lifetime_txns: Mapped[int] = mapped_column(Integer, nullable=False)
    min_age_days: Mapped[int] = mapped_column(Integer, nullable=False)
    micropayment_fee: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    small_payment_pct: Mapped[float] = mapped_column(Numeric(5, 3), nullable=False)
    large_payment_pct: Mapped[float] = mapped_column(Numeric(5, 3), nullable=False)
    cross_chain_surcharge: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    daily_limit: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    single_txn_limit: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    batch_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    credit_line: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    points_multiplier: Mapped[float] = mapped_column(Numeric(3, 1), default=1.0)
    features: Mapped[dict] = mapped_column(JSONB, default=dict)


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_agent_id: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    referred_agent_id: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    referral_code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_earned: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    unique_counterparties: Mapped[int] = mapped_column(Integer, default=0)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReferralEarning(Base):
    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referral_id: Mapped[int] = mapped_column(Integer, ForeignKey("referrals.id"))
    payment_id: Mapped[str] = mapped_column(String(66), nullable=False)
    payment_fee: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    referrer_share: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    credited_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AgentPoints(Base):
    __tablename__ = "agent_points"

    agent_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    current_points: Mapped[int] = mapped_column(BigInteger, default=0)
    lifetime_points: Mapped[int] = mapped_column(BigInteger, default=0)
    current_streak_days: Mapped[int] = mapped_column(Integer, default=0)
    longest_streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_active_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    multiplier: Mapped[float] = mapped_column(Numeric(3, 1), default=1.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PointEvent(Base):
    __tablename__ = "point_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(66), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    base_points: Mapped[int] = mapped_column(Integer, nullable=False)
    multiplier: Mapped[float] = mapped_column(Numeric(3, 1), nullable=False)
    total_points: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_point_events_agent_date", "agent_id", "created_at"),
    )

"""Reputation models — scores, snapshots, and on-chain anchors."""
import uuid
from datetime import datetime, date
from sqlalchemy import String, Integer, Numeric, DateTime, Date, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class ReputationScore(Base):
    __tablename__ = "reputation_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(66), unique=True, nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0)
    reliability: Mapped[float] = mapped_column(Numeric(5, 1), default=0)
    consistency: Mapped[float] = mapped_column(Numeric(5, 1), default=0)
    age_score: Mapped[float] = mapped_column(Numeric(5, 1), default=0)
    dispute_score: Mapped[float] = mapped_column(Numeric(5, 1), default=0)
    network_score: Mapped[float] = mapped_column(Numeric(5, 1), default=0)
    tier: Mapped[str] = mapped_column(String(20), default="NEW")
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReputationSnapshot(Base):
    __tablename__ = "reputation_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(66), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    on_chain_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)

    __table_args__ = (UniqueConstraint("agent_id", "snapshot_date"),)

"""Agent model — maps to agents table."""
import uuid
from datetime import datetime
from sqlalchemy import String, Numeric, Integer, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, JSONB
from .base import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agio_id: Mapped[str] = mapped_column(String(66), unique=True, nullable=False)
    wallet_address: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), default="NEW")
    balance: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    locked_balance: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    preferred_token: Mapped[str] = mapped_column(String(10), default="USDC")
    total_payments: Mapped[int] = mapped_column(Integer, default=0)
    total_volume: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentBalance(Base):
    """Per-token balance tracking. Each agent can hold multiple tokens."""
    __tablename__ = "agent_balances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(10), nullable=False)
    balance: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    locked_balance: Mapped[float] = mapped_column(Numeric(20, 6), default=0)

    __table_args__ = (
        Index("idx_agent_balances_agent_token", "agent_id", "token", unique=True),
    )

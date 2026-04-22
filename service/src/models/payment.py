"""Payment model — maps to payments table."""
import uuid
from datetime import datetime
from sqlalchemy import String, Numeric, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[str] = mapped_column(String(66), unique=True, nullable=False)
    from_agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    to_agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    amount: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="USDC")
    from_token: Mapped[str] = mapped_column(String(10), default="USDC")
    to_token: Mapped[str] = mapped_column(String(10), default="USDC")
    swap_fee: Mapped[float] = mapped_column(Numeric(20, 6), default=0)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    batch_id: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_payments_from", "from_agent_id"),
        Index("idx_payments_to", "to_agent_id"),
    )

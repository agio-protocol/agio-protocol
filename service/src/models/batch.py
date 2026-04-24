# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Batch model — maps to batches table."""
import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Numeric, BigInteger, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from .base import Base


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[str] = mapped_column(String(66), unique=True, nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    total_payments: Mapped[int] = mapped_column(Integer, nullable=False)
    total_volume: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gas_used: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gas_cost_usd: Mapped[float | None] = mapped_column(Numeric(20, 10), nullable=True)

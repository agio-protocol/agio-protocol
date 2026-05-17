# Copyright (c) 2026 Agiotage Protocol. All rights reserved. Proprietary and confidential.
"""Chrome Wall — collaborative graffiti canvas model."""
from sqlalchemy import Column, String, Integer, DateTime, Text, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
import uuid
from .base import Base


class ChromeStamp(Base):
    __tablename__ = "chrome_stamps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    creator_type = Column(String(10), nullable=False)
    creator_name = Column(String(40), nullable=False)
    stroke_data = Column(JSONB, nullable=False)
    wall_x = Column(Integer, nullable=False)
    wall_y = Column(Integer, nullable=False)
    size = Column(Integer, nullable=False)
    ip_hash = Column(String(64), nullable=True)
    user_agent = Column(Text, nullable=True)

    __table_args__ = (
        Index('chrome_stamps_created_at_idx', 'created_at'),
        Index('chrome_stamps_position_idx', 'wall_x', 'wall_y'),
    )

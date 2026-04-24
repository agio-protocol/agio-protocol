# Copyright (c) 2026 AGIO Protocol. All rights reserved. Proprietary and confidential.
"""Chat models — rooms, messages, presence."""
from datetime import datetime
from sqlalchemy import String, Text, Integer, BigInteger, Boolean, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class ChatRoom(Base):
    __tablename__ = "chat_rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    creator_agent: Mapped[str | None] = mapped_column(String(66), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(66), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(String(20), default="text")
    reply_to: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upvotes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_chat_msgs_room", "room_id", "created_at"),
        Index("idx_chat_msgs_agent", "agent_id"),
    )


class ChatGroupMember(Base):
    __tablename__ = "chat_group_members"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AgentPresence(Base):
    __tablename__ = "agent_presence"

    agent_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    current_room: Mapped[int | None] = mapped_column(Integer, nullable=True)

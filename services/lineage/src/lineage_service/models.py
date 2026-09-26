"""Lineage storage schema (SPEC §5.4)."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lineage_service.db import Base

Json = JSON().with_variant(JSONB(), "postgresql")


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(12), default="running")  # running|ok|error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input: Mapped[Any] = mapped_column(Json, nullable=True)
    output: Mapped[Any] = mapped_column(Json, nullable=True)
    attrs: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    replay_of: Mapped[str | None] = mapped_column(String(36), index=True)
    replay_mode: Mapped[str | None] = mapped_column(String(12))
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    steps: Mapped[list["Step"]] = relationship(
        back_populates="run", order_by="Step.seq", cascade="all, delete-orphan", passive_deletes=True
    )


class Step(Base):
    __tablename__ = "steps"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))  # llm|tool|input|output
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(12), default="ok")
    input: Mapped[Any] = mapped_column(Json, nullable=True)
    output: Mapped[Any] = mapped_column(Json, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    run: Mapped[Run] = relationship(back_populates="steps")
    __table_args__ = (Index("ix_steps_run_seq", "run_id", "seq", unique=True),)


class StepValue(Base):
    __tablename__ = "step_values"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    step_id: Mapped[str] = mapped_column(String(64), ForeignKey("steps.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(3))  # in|out
    path: Mapped[str] = mapped_column(String(300))
    fingerprint: Mapped[str] = mapped_column(String(16))
    display: Mapped[str] = mapped_column(String(300))
    __table_args__ = (Index("ix_values_run_fp", "run_id", "fingerprint"),)


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str] = mapped_column(String(64), ForeignKey("steps.id", ondelete="CASCADE"), index=True)
    chosen: Mapped[str] = mapped_column(String(300))
    chosen_fingerprint: Mapped[str] = mapped_column(String(16))
    alternatives: Mapped[list[str]] = mapped_column(Json, default=list)
    reason: Mapped[str | None] = mapped_column(Text)


class IngestedEvent(Base):
    """Idempotency ledger: event IDs already applied."""

    __tablename__ = "ingested_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(12), index=True)
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(12), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(Json)

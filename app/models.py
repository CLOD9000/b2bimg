"""Modelli ORM del portale."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    """Timestamp corrente in UTC (naive, coerente con SQLite)."""
    return datetime.now(UTC).replace(tzinfo=None)


class JobStatus(str, enum.Enum):
    DRAFT = "draft"          # CSV importato, manca config/filtro
    READY = "ready"          # configurato, in attesa di avvio
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RowStatus(str, enum.Enum):
    PENDING = "pending"
    SELECTED = "selected"
    SKIPPED = "skipped"      # escluso dal filtro
    DOWNLOADED = "downloaded"
    NOT_FOUND = "not_found"
    ERROR = "error"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    user: Mapped[User] = relationship(back_populates="sessions")


class LoginAttempt(Base):
    """Finestra scorciatoia per il rate limiting del login."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    """Un job = un CSV importato + la sua configurazione + la sua esecuzione."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    source_filename: Mapped[str] = mapped_column(String(255), default="")
    encoding: Mapped[str] = mapped_column(String(32), default="utf-8")
    delimiter: Mapped[str] = mapped_column(String(8), default=",")
    headers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    code_field: Mapped[str] = mapped_column(String(200), default="")

    status: Mapped[str] = mapped_column(String(24), default=JobStatus.DRAFT.value)
    filter_field: Mapped[str] = mapped_column(String(200), default="")
    filter_values: Mapped[list[Any]] = mapped_column(JSON, default=list)

    source_type: Mapped[str] = mapped_column(String(64), default="")
    source_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dest_type: Mapped[str] = mapped_column(String(64), default="")
    dest_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    filename_template: Mapped[str] = mapped_column(String(200), default="{codice}.jpg")
    conflict_strategy: Mapped[str] = mapped_column(String(16), default="rename")

    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    rows: Mapped[list[JobRow]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobRow.idx"
    )

    @property
    def counts(self) -> dict[str, int]:
        """Conteggi per stato (usati da progress bar e dashboard)."""
        out = {s.value: 0 for s in RowStatus}
        for row in self.rows:
            out[row.status] = out.get(row.status, 0) + 1
        return out


class JobRow(Base):
    __tablename__ = "job_rows"
    __table_args__ = (UniqueConstraint("job_id", "idx", name="uq_job_row_idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    code: Mapped[str] = mapped_column(String(255), default="", index=True)

    status: Mapped[str] = mapped_column(String(24), default=RowStatus.PENDING.value, index=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    source_used: Mapped[str] = mapped_column(String(64), default="")
    resolved_location: Mapped[str] = mapped_column(String(1024), default="")
    output_path: Mapped[str] = mapped_column(String(1024), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="rows")

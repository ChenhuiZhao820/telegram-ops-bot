"""Database-backed task queue models. One Postgres, no broker."""

import os
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def _database_url() -> str:
    url = os.environ["DATABASE_URL"]
    # Render hands out postgres:// URLs; SQLAlchemy needs an explicit driver.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    """SQLite (local dev) returns naive datetimes; Postgres returns aware ones."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(64))
    telegram_user_id: Mapped[str] = mapped_column(String(64))
    instruction: Mapped[str] = mapped_column(Text)
    # queued / running / awaiting_confirmation / done / failed / cancelled / expired
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    conversation_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pending_tool_call: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tools_called: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Telegram message_id of the "Thinking…" placeholder, deleted when replying.
    ack_message_id: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OtterToken(Base):
    """Single-row store for Otter OAuth tokens. Otter rotates the refresh token
    on every use, so tokens must outlive process restarts (env vars can't).
    seed_refresh_token remembers which env-provided token seeded this row: when
    a human re-auths and updates the env var, the row is reseeded from env."""
    __tablename__ = "otter_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    seed_refresh_token: Mapped[str] = mapped_column(Text)
    seed_access_token: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DevinSession(Base):
    __tablename__ = "devin_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(128), unique=True)
    task_description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64))
    # Chat to notify when the session changes state; watcher bookkeeping.
    chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(_database_url(), pool_pre_ping=True)
    return _engine


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    # Best-effort migration for deployments created before seed_access_token
    # existed (create_all never alters existing tables).
    from sqlalchemy import text

    for ddl in ("ALTER TABLE otter_tokens ADD COLUMN seed_access_token TEXT DEFAULT ''",
                "ALTER TABLE tasks ADD COLUMN ack_message_id INTEGER",
                "ALTER TABLE devin_sessions ADD COLUMN chat_id VARCHAR(64)",
                "ALTER TABLE devin_sessions ADD COLUMN last_status TEXT"):
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
        except Exception:
            pass  # column already exists


def db_session() -> Session:
    return Session(get_engine())

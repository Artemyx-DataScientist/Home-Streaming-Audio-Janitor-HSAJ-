from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from ..timeutils import ensure_utc


class UtcDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        normalized = ensure_utc(value)
        if normalized is None:
            return None
        dialect_name = getattr(dialect, "name", None)
        if dialect_name == "sqlite":
            return normalized.replace(tzinfo=None)
        return normalized

    def process_result_value(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        return ensure_utc(value)


class Base(DeclarativeBase):
    """Base class for declarative ORM models."""


class File(Base):
    """Physical file in the library."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mtime: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    artist: Mapped[str | None] = mapped_column(String(512), nullable=True)
    album: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    atmos_detected: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ActionLog(Base):
    """Audit log for filesystem and planning actions."""

    __tablename__ = "actions_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), nullable=False
    )


class PlayHistory(Base):
    """Playback history records."""

    __tablename__ = "play_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    quality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    played_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artist: Mapped[str | None] = mapped_column(String(512), nullable=True)
    album: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), nullable=False
    )


class RoonItemCache(Base):
    """Cached Roon track metadata."""

    __tablename__ = "roon_items_cache"

    roon_track_id: Mapped[str] = mapped_column(
        String(512),
        primary_key=True,
        nullable=False,
    )
    artist: Mapped[str | None] = mapped_column(String(512), nullable=True)
    album: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RoonBlockRaw(Base):
    """Raw blocked objects imported from Roon."""

    __tablename__ = "roon_blocks_raw"
    __table_args__ = (
        UniqueConstraint("object_type", "object_id", name="ux_roon_block_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BlockCandidate(Base):
    """Candidate produced from blocked Roon objects."""

    __tablename__ = "block_candidates"
    __table_args__ = (
        UniqueConstraint("object_type", "object_id", name="ux_block_candidate_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(512), nullable=False)
    label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    planned_action_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    restored_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

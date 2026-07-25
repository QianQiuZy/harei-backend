from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Song(Base):
    __tablename__ = "songs"
    song_id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    artist: Mapped[str] = mapped_column(String(500))
    artists: Mapped[list[str]] = mapped_column(JSON)
    genre: Mapped[str] = mapped_column(String(100), index=True)
    language: Mapped[str] = mapped_column(String(50), index=True)
    work_type: Mapped[str] = mapped_column(String(50), index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    metadata_status: Mapped[str] = mapped_column(String(30), default="complete")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class SongPerformance(Base):
    __tablename__ = "song_performances"
    performance_id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.song_id", ondelete="CASCADE"), index=True)
    performed_on: Mapped[date] = mapped_column(Date)
    platform: Mapped[str] = mapped_column(String(100))
    stream_id: Mapped[str | None] = mapped_column(String(100))
    stream_title: Mapped[str | None] = mapped_column(String(255))
    stream_url: Mapped[str | None] = mapped_column(String(2048))
    clip_url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class MusicCatalogRevision(Base):
    __tablename__ = "music_catalog_revision"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class MusicAuditEvent(Base):
    __tablename__ = "music_audit_events"
    audit_id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(100), index=True)
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

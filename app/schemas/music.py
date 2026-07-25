from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, JsonValue, field_validator
from pydantic_core import PydanticCustomError

class StreamModel(BaseModel):
    id: str | None = None
    title: str | None = None
    platform: str
    url: str | None = None

class PerformanceOut(BaseModel):
    performance_id: int
    id: str
    date: date
    stream: StreamModel
    clipUrl: str | None = None

class SongSummary(BaseModel):
    song_id: int
    id: str
    source_key: str
    title: str
    artist: str
    artists: list[str]
    genre: str
    language: str
    workType: str
    notes: str
    metadataStatus: str
    latestPerformanceAt: date | None = None
    latestLink: str | None = None
    performanceCount: int

class SongDetail(SongSummary):
    performances: list[PerformanceOut]

class MusicListResponse(BaseModel):
    code: int = 0
    items: list[SongSummary]
    total: int
    page: int
    page_size: int
    facets: dict[str, list[str]]
    stats: dict[str, int]
    revision: int

class SongInput(BaseModel):
    title: str
    artist: str
    artists: list[str]
    genre: str
    language: str
    work_type: str
    notes: str = ""
    metadata_status: str = "complete"

class SongUpdate(BaseModel):
    version: int
    title: str | None = None
    artist: str | None = None
    artists: list[str] | None = None
    genre: str | None = None
    language: str | None = None
    work_type: str | None = None
    notes: str | None = None
    metadata_status: str | None = None

class VersionInput(BaseModel):
    version: int

class PerformanceInput(BaseModel):
    version: int = Field(ge=1)
    date: date
    platform: str
    stream_title: str | None = None
    stream_url: str | None = None
    clip_url: str | None = None

    @field_validator("stream_url", "clip_url")
    @classmethod
    def validate_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PydanticCustomError("http_url", "URL must use http or https")
        return value

class AuditOut(BaseModel):
    audit_id: int
    actor: str
    action: str
    entity_type: str
    entity_id: str
    details: dict[str, JsonValue]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

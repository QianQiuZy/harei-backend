from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.music import Music
from app.schemas.music import MusicItem, MusicListResponse

router = APIRouter()

_CACHE_TTL = timedelta(hours=24)
_cache_lock = asyncio.Lock()
_cache_items: list[MusicItem] | None = None
_cache_updated_at: datetime | None = None


async def _load_music(session: AsyncSession) -> list[MusicItem]:
    result = await session.execute(select(Music))
    rows = result.scalars().all()
    return [
        MusicItem(
            music_id=row.music_id,
            title=row.title,
            artist=row.artist,
            type=row.type,
            language=row.language,
            note=row.note,
        )
        for row in rows
    ]


@router.get("/music", response_model=MusicListResponse)
async def list_music(session: AsyncSession = Depends(get_db_session)) -> MusicListResponse:
    global _cache_items, _cache_updated_at

    now = datetime.utcnow()
    if _cache_items is not None and _cache_updated_at and now - _cache_updated_at < _CACHE_TTL:
        return MusicListResponse(items=_cache_items)

    async with _cache_lock:
        if _cache_items is not None and _cache_updated_at and now - _cache_updated_at < _CACHE_TTL:
            return MusicListResponse(items=_cache_items)
        items = await _load_music(session)
        _cache_items = items
        _cache_updated_at = now
        return MusicListResponse(items=items)

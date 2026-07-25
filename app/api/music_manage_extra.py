from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.music_manage import _changed, _song_or_404
from app.db.session import get_db_session
from app.deps.auth import Principal, require_music_manage
from app.models.music import MusicAuditEvent, Song, SongPerformance
from app.schemas.music import AuditOut, PerformanceInput
from app.services.music_identifiers import derive_stream_id, generate_music_source_key

router = APIRouter(prefix="/music-manage")


async def _bump_song_version(session: AsyncSession, song_id: int, version: int) -> int:
    result = await session.execute(
        update(Song)
        .where(Song.song_id == song_id, Song.version == version)
        .values(version=Song.version + 1)
    )
    if not result.rowcount:
        await _song_or_404(session, song_id)
        raise HTTPException(status_code=409, detail="Version conflict")
    return version + 1

@router.post("/songs/{song_id}/performances", status_code=201)
async def create_performance(song_id: int, payload: PerformanceInput, principal: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]:
    version = await _bump_song_version(session, song_id, payload.version)
    row = SongPerformance(song_id=song_id, performed_on=payload.date, platform=payload.platform, source_key=generate_music_source_key("performance"), stream_id=derive_stream_id(payload.stream_url, payload.clip_url), stream_title=payload.stream_title, stream_url=payload.stream_url, clip_url=payload.clip_url)
    session.add(row)
    try: await session.flush()
    except IntegrityError as exc:
        await session.rollback(); raise HTTPException(status_code=409, detail="Duplicate source_key") from exc
    revision = await _changed(session, principal.subject, "performance.created", "performance", row.source_key, {"after": {"song_id": song_id, "date": str(row.performed_on), "platform": row.platform}})
    await session.commit()
    return {"code": 0, "performance_id": row.performance_id, "source_key": row.source_key, "version": version, "revision": revision}

@router.put("/performances/{performance_id}")
async def update_performance(performance_id: int, payload: PerformanceInput, principal: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]:
    row = await session.get(SongPerformance, performance_id)
    if row is None: raise HTTPException(status_code=404, detail="Performance not found")
    version = await _bump_song_version(session, row.song_id, payload.version)
    before = {"source_key": row.source_key, "date": str(row.performed_on), "platform": row.platform, "clip_url": row.clip_url}
    try:
        row.performed_on, row.platform = payload.date, payload.platform
        row.stream_id, row.stream_title, row.stream_url, row.clip_url = derive_stream_id(payload.stream_url, payload.clip_url), payload.stream_title, payload.stream_url, payload.clip_url
        await session.flush()
    except IntegrityError as exc:
        await session.rollback(); raise HTTPException(status_code=409, detail="Duplicate source_key") from exc
    revision = await _changed(session, principal.subject, "performance.updated", "performance", row.source_key, {"before": before, "after": {"source_key": row.source_key, "date": str(row.performed_on), "platform": row.platform, "clip_url": row.clip_url}})
    await session.commit(); return {"code": 0, "version": version, "revision": revision}

@router.delete("/performances/{performance_id}")
async def delete_performance(performance_id: int, version: int = Query(ge=1), principal: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]:
    row = await session.get(SongPerformance, performance_id)
    if row is None: raise HTTPException(status_code=404, detail="Performance not found")
    source_key, song_id = row.source_key, row.song_id
    next_version = await _bump_song_version(session, song_id, version)
    await session.delete(row)
    revision = await _changed(session, principal.subject, "performance.deleted", "performance", source_key, {"before": {"song_id": song_id, "source_key": source_key}})
    await session.commit(); return {"code": 0, "version": next_version, "revision": revision}

@router.get("/audit")
async def audit(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), _: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]:
    total = await session.scalar(select(func.count()).select_from(MusicAuditEvent)) or 0
    rows = list((await session.scalars(select(MusicAuditEvent).order_by(MusicAuditEvent.audit_id.desc()).offset((page - 1) * page_size).limit(page_size))).all())
    return {"code": 0, "items": [AuditOut.model_validate(row, from_attributes=True).model_dump() for row in rows], "total": total, "page": page, "page_size": page_size}

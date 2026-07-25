from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.music import _performance, _summary
from app.core.config import get_settings
from app.core.redis import get_redis_client
from app.db.session import get_db_session
from app.deps.auth import Principal, require_music_manage
from app.models.music import MusicAuditEvent, MusicCatalogRevision, Song, SongPerformance
from app.schemas.auth import LoginRequest, LoginResponse, UserInfo
from app.schemas.music import AuditOut, PerformanceInput, SongInput, SongUpdate, VersionInput
from app.services.auth_service import AuthService
from app.services.music_identifiers import generate_music_source_key

router = APIRouter(prefix="/music-manage")

async def _changed(session: AsyncSession, actor: str, action: str, entity_type: str, entity_id: str, details: dict[str, object]) -> int:
    await session.execute(update(MusicCatalogRevision).where(MusicCatalogRevision.id == 1).values(revision=MusicCatalogRevision.revision + 1))
    session.add(MusicAuditEvent(actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, details=details))
    await session.flush()
    return await session.scalar(select(MusicCatalogRevision.revision).where(MusicCatalogRevision.id == 1)) or 0

async def _song_or_404(session: AsyncSession, song_id: int) -> Song:
    song = await session.get(Song, song_id)
    if song is None: raise HTTPException(status_code=404, detail="Song not found")
    return song

@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, redis: Redis = Depends(get_redis_client)) -> LoginResponse:
    service = AuthService(redis)
    if not service.verify_music_credentials(payload.username, payload.password): raise HTTPException(status_code=401, detail="Unauthorized")
    token, principal = await service.issue_token(payload.username, ["music:manage"], get_settings().music_token_ttl_seconds)
    return LoginResponse(token=token, user=UserInfo(username=payload.username), scopes=principal.scopes, expires_at=principal.expires_at)

@router.get("/stats")
async def stats(_: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]:
    active = await session.scalar(select(func.count()).select_from(Song).where(Song.status == "active")) or 0
    archived = await session.scalar(select(func.count()).select_from(Song).where(Song.status == "archived")) or 0
    performances = await session.scalar(select(func.count()).select_from(SongPerformance)) or 0
    revision = await session.scalar(select(MusicCatalogRevision.revision).where(MusicCatalogRevision.id == 1)) or 0
    return {"code": 0, "activeSongs": active, "archivedSongs": archived, "performanceCount": performances, "revision": revision}

@router.get("/songs")
async def songs(q: str | None = None, status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100), _: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]:
    stmt = select(Song)
    if q: stmt = stmt.where(Song.title.ilike(f"%{q}%"))
    if status: stmt = stmt.where(Song.status == status)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list((await session.scalars(stmt.order_by(Song.updated_at.desc()).offset((page - 1) * page_size).limit(page_size))).all())
    return {"code": 0, "items": [{"song_id": row.song_id, "source_key": row.source_key, "title": row.title, "artist": row.artist, "status": row.status, "version": row.version} for row in rows], "total": total, "page": page, "page_size": page_size}

@router.post("/songs", status_code=201)
async def create_song(payload: SongInput, principal: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]:
    song = Song(source_key=generate_music_source_key("song"), **payload.model_dump())
    session.add(song)
    try: await session.flush()
    except IntegrityError as exc:
        await session.rollback(); raise HTTPException(status_code=409, detail="Duplicate source_key") from exc
    revision = await _changed(session, principal.subject, "song.created", "song", song.source_key, {})
    await session.commit()
    return {"code": 0, "song_id": song.song_id, "source_key": song.source_key, "version": song.version, "revision": revision}

@router.get("/songs/{song_id}")
async def song_detail(song_id: int, _: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]:
    song = await _song_or_404(session, song_id)
    performances = list((await session.scalars(select(SongPerformance).where(SongPerformance.song_id == song_id).order_by(SongPerformance.performed_on.desc()))).all())
    return {"code": 0, "item": {**_summary(song, len(performances), performances[0] if performances else None).model_dump(), "song_id": song.song_id, "status": song.status, "version": song.version, "performances": [_performance(item).model_dump() for item in performances]}}

@router.put("/songs/{song_id}")
async def update_song(song_id: int, payload: SongUpdate, principal: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]:
    values = payload.model_dump(exclude_unset=True, exclude={"version"})
    result = await session.execute(update(Song).where(Song.song_id == song_id, Song.version == payload.version).values(**values, version=Song.version + 1))
    if not result.rowcount:
        await _song_or_404(session, song_id); raise HTTPException(status_code=409, detail="Version conflict")
    song = await _song_or_404(session, song_id)
    revision = await _changed(session, principal.subject, "song.updated", "song", song.source_key, {"changed": values})
    await session.commit(); await session.refresh(song)
    return {"code": 0, "version": song.version, "revision": revision}

async def _set_status(song_id: int, payload: VersionInput, desired: str, action: str, principal: Principal, session: AsyncSession) -> dict[str, object]:
    before_status = (await _song_or_404(session, song_id)).status
    result = await session.execute(update(Song).where(Song.song_id == song_id, Song.version == payload.version).values(status=desired, version=Song.version + 1))
    if not result.rowcount:
        await _song_or_404(session, song_id); raise HTTPException(status_code=409, detail="Version conflict")
    song = await _song_or_404(session, song_id); revision = await _changed(session, principal.subject, action, "song", song.source_key, {"before": before_status, "after": desired})
    await session.commit(); return {"code": 0, "revision": revision}

@router.post("/songs/{song_id}/archive")
async def archive(song_id: int, payload: VersionInput, principal: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]: return await _set_status(song_id, payload, "archived", "song.archived", principal, session)
@router.post("/songs/{song_id}/restore")
async def restore(song_id: int, payload: VersionInput, principal: Principal = Depends(require_music_manage), session: AsyncSession = Depends(get_db_session)) -> dict[str, object]: return await _set_status(song_id, payload, "active", "song.restored", principal, session)

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.music import MusicCatalogRevision, Song, SongPerformance
from app.schemas.music import MusicListResponse, PerformanceOut, SongDetail, SongSummary, StreamModel

router = APIRouter()


def _summary(song: Song, count: int, latest: SongPerformance | None) -> SongSummary:
    return SongSummary(
        song_id=song.song_id,
        id=song.source_key,
        source_key=song.source_key,
        title=song.title,
        artist=song.artist,
        artists=song.artists,
        genre=song.genre,
        language=song.language,
        workType=song.work_type,
        notes=song.notes,
        metadataStatus=song.metadata_status,
        latestPerformanceAt=latest.performed_on if latest else None,
        latestLink=latest.clip_url if latest else None,
        performanceCount=count,
    )


def _performance(row: SongPerformance) -> PerformanceOut:
    return PerformanceOut(
        performance_id=row.performance_id,
        id=row.source_key,
        date=row.performed_on,
        stream=StreamModel(
            id=row.stream_id,
            title=row.stream_title,
            platform=row.platform,
            url=row.stream_url,
        ),
        clipUrl=row.clip_url,
    )


async def _catalog_revision(response: Response, session: AsyncSession) -> int:
    revision = (
        await session.scalar(
            select(MusicCatalogRevision.revision).where(MusicCatalogRevision.id == 1)
        )
        or 0
    )
    response.headers["ETag"] = f'W/"music-{revision}"'
    return revision


@router.get("/music", response_model=MusicListResponse)
async def list_music(
    request: Request,
    response: Response,
    q: str | None = None,
    search_mode: str = Query("title", pattern="^(title|artist)$"),
    genre: str | None = None,
    language: str | None = None,
    work_type: str | None = None,
    sort: str = Query("title", pattern="^(title|recent|count)$"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
) -> MusicListResponse | Response:
    revision = await _catalog_revision(response, session)
    etag = response.headers["ETag"]
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    filters = [Song.status == "active"]
    if q:
        search_field = Song.artist if search_mode == "artist" else Song.title
        filters.append(search_field.ilike(f"%{q}%"))
    if genre:
        filters.append(Song.genre == genre)
    if language:
        filters.append(Song.language == language)
    if work_type:
        filters.append(Song.work_type == work_type)

    total = await session.scalar(select(func.count()).select_from(Song).where(*filters)) or 0
    count_subquery = (
        select(func.count(SongPerformance.performance_id))
        .where(SongPerformance.song_id == Song.song_id)
        .correlate(Song)
        .scalar_subquery()
    )
    latest_id_subquery = (
        select(SongPerformance.performance_id)
        .where(SongPerformance.song_id == Song.song_id)
        .order_by(SongPerformance.performed_on.desc(), SongPerformance.performance_id.desc())
        .limit(1)
        .correlate(Song)
        .scalar_subquery()
    )
    latest_date_subquery = (
        select(SongPerformance.performed_on)
        .where(SongPerformance.song_id == Song.song_id)
        .order_by(SongPerformance.performed_on.desc())
        .limit(1)
        .correlate(Song)
        .scalar_subquery()
    )
    ordering_fields = {
        "title": Song.title,
        "recent": latest_date_subquery,
        "count": count_subquery,
    }
    ordering = ordering_fields[sort]
    ordering = ordering.desc() if order == "desc" else ordering.asc()
    rows = (
        await session.execute(
            select(Song, count_subquery, SongPerformance)
            .outerjoin(SongPerformance, SongPerformance.performance_id == latest_id_subquery)
            .where(*filters)
            .order_by(ordering, Song.song_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    active_filter = Song.status == "active"
    facets = {
        "genres": list(
            (await session.scalars(select(Song.genre).where(active_filter).distinct().order_by(Song.genre))).all()
        ),
        "languages": list(
            (
                await session.scalars(
                    select(Song.language).where(active_filter).distinct().order_by(Song.language)
                )
            ).all()
        ),
        "workTypes": list(
            (
                await session.scalars(
                    select(Song.work_type).where(active_filter).distinct().order_by(Song.work_type)
                )
            ).all()
        ),
    }
    active_total = await session.scalar(select(func.count()).select_from(Song).where(active_filter)) or 0
    performance_total = (
        await session.scalar(
            select(func.count()).select_from(SongPerformance).join(Song).where(active_filter)
        )
        or 0
    )
    return MusicListResponse(
        items=[_summary(song, count, latest) for song, count, latest in rows],
        total=total,
        page=page,
        page_size=page_size,
        facets=facets,
        stats={"song_count": active_total, "performance_count": performance_total},
        revision=revision,
    )


@router.get("/music/export")
async def export_music(
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    revision = await _catalog_revision(response, session)
    songs = list(
        (await session.scalars(select(Song).where(Song.status == "active").order_by(Song.title))).all()
    )
    performances = list(
        (
            await session.scalars(
                select(SongPerformance)
                .join(Song)
                .where(Song.status == "active")
                .order_by(SongPerformance.performed_on.desc())
            )
        ).all()
    )
    grouped: dict[int, list[SongPerformance]] = {}
    for performance in performances:
        grouped.setdefault(performance.song_id, []).append(performance)
    items = []
    for song in songs:
        history = grouped.get(song.song_id, [])
        items.append(
            SongDetail(
                **_summary(song, len(history), history[0] if history else None).model_dump(),
                performances=[_performance(row) for row in history],
            ).model_dump()
        )
    return {
        "code": 0,
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat(),
        "revision": revision,
        "songs": items,
    }


@router.get("/music/{song_id}")
async def get_music(
    song_id: str,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int | SongDetail]:
    await _catalog_revision(response, session)
    song = await session.scalar(
        select(Song).where(Song.source_key == song_id, Song.status == "active")
    )
    if song is None:
        raise HTTPException(status_code=404, detail="Song not found")
    performances = list(
        (
            await session.scalars(
                select(SongPerformance)
                .where(SongPerformance.song_id == song.song_id)
                .order_by(SongPerformance.performed_on.desc(), SongPerformance.performance_id.desc())
            )
        ).all()
    )
    summary = _summary(song, len(performances), performances[0] if performances else None)
    return {
        "code": 0,
        "item": SongDetail(
            **summary.model_dump(),
            performances=[_performance(row) for row in performances],
        ),
    }

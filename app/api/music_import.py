from collections import Counter, defaultdict
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.deps.auth import Principal, require_music_manage
from app.models.music import MusicAuditEvent, MusicCatalogRevision, Song, SongPerformance
from app.services.music_identifiers import derive_stream_id, generate_music_source_key
from app.services.music_workbook import WorkbookIssue, build_performance_template, parse_performance_workbook


router = APIRouter(prefix="/music-manage/performances")
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MusicPrincipalDep = Annotated[Principal, Depends(require_music_manage)]
DatabaseSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
WorkbookUploadDep = Annotated[UploadFile, File()]


@router.get("/template")
async def performance_template(
    _: MusicPrincipalDep,
    session: DatabaseSessionDep,
) -> StreamingResponse:
    songs = list((await session.scalars(select(Song).order_by(Song.title, Song.song_id))).all())
    workbook = build_performance_template(songs)
    return StreamingResponse(
        workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="harei-performance-import-template.xlsx"'},
    )


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_performances(
    file: WorkbookUploadDep,
    principal: MusicPrincipalDep,
    session: DatabaseSessionDep,
) -> dict[str, int]:
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise _validation_error([WorkbookIssue(1, "file", "INVALID_EXTENSION", "只支持 .xlsx 文件")])
    contents = await file.read(MAX_IMPORT_BYTES + 1)
    if len(contents) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail={"error": "file_too_large", "max_bytes": MAX_IMPORT_BYTES})
    rows, issues = parse_performance_workbook(contents)
    if issues:
        raise _validation_error(issues)

    titles = {row.song_title for row in rows}
    songs = list((await session.scalars(select(Song).where(Song.title.in_(titles)))).all())
    matches: dict[str, list[Song]] = defaultdict(list)
    for song in songs:
        matches[song.title].append(song)
    for row in rows:
        candidates = matches.get(row.song_title, [])
        if not candidates:
            issues.append(WorkbookIssue(row.excel_row, "歌名", "SONG_NOT_FOUND", "数据库中不存在完全同名歌曲"))
        elif len(candidates) > 1:
            issues.append(WorkbookIssue(row.excel_row, "歌名", "AMBIGUOUS_SONG_TITLE", "数据库中存在多首完全同名歌曲"))
    if issues:
        raise _validation_error(issues)

    song_counts: Counter[int] = Counter()
    for row in rows:
        song = matches[row.song_title][0]
        source_key = generate_music_source_key("performance")
        performance = SongPerformance(
            source_key=source_key,
            song_id=song.song_id,
            performed_on=row.performed_on,
            platform="哔哩哔哩",
            stream_id=derive_stream_id(None, row.clip_url),
            stream_title=row.stream_title,
            stream_url=None,
            clip_url=row.clip_url,
        )
        session.add(performance)
        session.add(
            MusicAuditEvent(
                actor=principal.subject,
                action="performance.imported",
                entity_type="performance",
                entity_id=source_key,
                details={"after": {"song_id": song.song_id, "date": str(row.performed_on), "platform": "哔哩哔哩"}},
            )
        )
        song_counts[song.song_id] += 1

    for song_id, count in song_counts.items():
        _ = await session.execute(update(Song).where(Song.song_id == song_id).values(version=Song.version + count))
    _ = await session.execute(
        update(MusicCatalogRevision)
        .where(MusicCatalogRevision.id == 1)
        .values(revision=MusicCatalogRevision.revision + 1)
    )
    await session.flush()
    revision = await session.scalar(
        select(MusicCatalogRevision.revision).where(MusicCatalogRevision.id == 1)
    ) or 0
    await session.commit()
    return {
        "code": 0,
        "imported_count": len(rows),
        "affected_song_count": len(song_counts),
        "revision": revision,
    }


def _validation_error(issues: list[WorkbookIssue]) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"error": "invalid_workbook", "errors": [issue.as_dict() for issue in issues]},
    )

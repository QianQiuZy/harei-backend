from __future__ import annotations

from asyncio import to_thread
from contextlib import ExitStack
from pathlib import Path
from shutil import copyfileobj
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.datastructures import UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.hot_cache import public_cache
from app.db.session import get_db_session
from app.deps.auth import require_admin
from app.models.download import Download
from app.schemas.download import DownloadAddResponse, DownloadListResponse

router = APIRouter(prefix="/download")

DOWNLOAD_ROOT = Path("download_files")


def _is_external_path(path: str) -> bool:
    return path.startswith("http://") or path.startswith("https://")


async def require_token(
    _: object = Depends(require_admin),
) -> None:
    return None


def _resolve_download_path(path: str) -> Path:
    base_dir = DOWNLOAD_ROOT.resolve()
    candidate = Path(path)
    full_path = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    if base_dir not in full_path.parents and full_path != base_dir:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not full_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return full_path


@router.get("/active", response_model=DownloadListResponse)
@public_cache("downloads")
async def list_active(
    session: AsyncSession = Depends(get_db_session),
) -> DownloadListResponse:
    result = await session.execute(select(Download).order_by(Download.download_id.desc()))
    rows = result.scalars().all()
    items = [
        {
            "download_id": row.download_id,
            "description": row.description,
            "path": row.path
            if _is_external_path(row.path)
            else f"/download/file?download_id={row.download_id}",
        }
        for row in rows
    ]
    return DownloadListResponse(items=items)


@router.get("/file", name="download_file")
async def download_file(
    download_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> FileResponse:
    row = await session.get(Download, download_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if _is_external_path(row.path):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="External path not allowed")
    file_path = _resolve_download_path(row.path)
    return FileResponse(
        file_path, filename=file_path.name,
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"},
    )


@router.post("/add", response_model=DownloadAddResponse)
async def add_download(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> DownloadAddResponse:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = await request.json()
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, detail="Invalid JSON object") from exc
        if not isinstance(payload, dict):
            raise HTTPException(422, detail="Expected a JSON object")
        return await _save_download(session, payload.get("description", ""), payload.get("path", ""))

    async with request.form() as form:
        upload = form.get("file")
        return await _save_download(
            session, form.get("description", ""), form.get("path", ""),
            upload if isinstance(upload, UploadFile) else None,
        )


async def _save_download(
    session: AsyncSession, description: object, download_path: object, upload_file: UploadFile | None = None,
) -> DownloadAddResponse:
    if not isinstance(description, str) or not isinstance(download_path, str):
        raise HTTPException(422, detail="Expected text for description and path")
    description = description.strip()
    download_path = download_path.strip()
    if len(description) > 255 or len(download_path) > 1024:
        raise HTTPException(422, detail="Description or path too long")
    missing_fields: list[str] = []
    if not description:
        missing_fields.append("description")
    if not download_path and upload_file is None:
        missing_fields.extend(["path", "file"])
    if missing_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"missing_fields": missing_fields},
        )
    if download_path and upload_file is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only one of path or file is allowed",
        )

    with ExitStack() as uncommitted_files:
        if upload_file is not None:
            DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
            suffix = Path(upload_file.filename or "").suffix.lower()
            if not suffix or len(suffix.encode()) > 223 or "\x00" in suffix:
                suffix = ".bin"
            file_path = DOWNLOAD_ROOT / f"{uuid4().hex}{suffix}"
            with file_path.open("xb") as target:
                uncommitted_files.callback(file_path.unlink, missing_ok=True)
                await to_thread(copyfileobj, upload_file.file, target, 1024 * 1024)
            download_path = str(file_path)

        row = Download(description=description, path=download_path)
        session.add(row)
        await session.flush()
        # Once COMMIT starts its acknowledgement can be lost even if the row was saved.
        uncommitted_files.pop_all()
    await session.commit()
    return DownloadAddResponse(download_id=row.download_id, path=row.path)

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis_client
from app.db.session import get_db_session
from app.deps.auth import get_bearer_token
from app.models.download import Download
from app.schemas.download import DownloadAddResponse, DownloadListResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/download")

DOWNLOAD_ROOT = Path("download_files")


def _is_external_path(path: str) -> bool:
    return path.startswith("http://") or path.startswith("https://")


async def require_token(
    token: str = Depends(get_bearer_token),
    redis: Redis = Depends(get_redis_client),
) -> None:
    service = AuthService(redis)
    username = await service.get_username_by_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


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
async def list_active(
    request: Request,
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
            else f"{request.url_for('download_file')}?download_id={row.download_id}",
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
    return FileResponse(file_path)


@router.post("/add", response_model=DownloadAddResponse)
async def add_download(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> DownloadAddResponse:
    content_type = request.headers.get("content-type", "")
    description = ""
    download_path = ""
    upload_file: UploadFile | None = None

    if "application/json" in content_type:
        payload = await request.json()
        description = str(payload.get("description") or "").strip()
        download_path = str(payload.get("path") or "").strip()
    else:
        form = await request.form()
        description = str(form.get("description") or "").strip()
        download_path = str(form.get("path") or "").strip()
        file_value = form.get("file")
        if isinstance(file_value, UploadFile):
            upload_file = file_value

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

    if upload_file is not None:
        DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        file_suffix = Path(upload_file.filename or "").suffix.lower() or ".bin"
        file_id = uuid4().hex
        file_path = DOWNLOAD_ROOT / f"{file_id}{file_suffix}"
        raw_bytes = await upload_file.read()
        file_path.write_bytes(raw_bytes)
        download_path = str(file_path)

    row = Download(description=description, path=download_path)
    session.add(row)
    await session.flush()
    await session.commit()
    return DownloadAddResponse(download_id=row.download_id, path=row.path)

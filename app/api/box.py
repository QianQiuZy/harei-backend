from __future__ import annotations

from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
import asyncio
from typing import Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image as PilImage, UnidentifiedImageError
from pillow_heif import register_heif_opener
from sqlalchemy import update, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.rate_limit import BEIJING_WINDOW, client_ip
from app.core.redis import get_rate_limit_redis
from app.db.session import get_db_session
from app.deps.auth import require_admin
from app.models.image import Image
from app.models.message import Message
from app.schemas.box import DeleteRequest, MessageListResponse, TagFilterRequest, UploadResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/box")


class AsyncScriptRedis(Protocol):
    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str,
    ) -> list[int]: ...

UPLOAD_ROOT = Path("uploads")
ORIGINAL_DIR = UPLOAD_ROOT / "original"
THUMB_DIR = UPLOAD_ROOT / "thumbs"
JPG_DIR = UPLOAD_ROOT / "jpg"

register_heif_opener()

IMAGE_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
    "GIF": (".gif", "image/gif"),
    "BMP": (".bmp", "image/bmp"),
    "TIFF": (".tiff", "image/tiff"),
    "HEIF": (".heic", "image/heic"),
}
MAX_UPLOAD_FILES = 6
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = 50 * 1024 * 1024
MAX_DECODED_IMAGE_PIXELS = 25_000_000

RATE_LIMIT_SCRIPT = BEIJING_WINDOW + """
local key, seq = KEYS[1], KEYS[2]
redis.call('ZREMRANGEBYSCORE', key, '-inf', math.min(now - 3600, day_start - 1))

local function rolling_retry(seconds, maximum)
    local start = now - seconds + 1
    if redis.call('ZCOUNT', key, start, now) < maximum then return 0 end
    local first = redis.call('ZRANGEBYSCORE', key, start, now, 'WITHSCORES', 'LIMIT', 0, 1)
    return tonumber(first[2]) + seconds
end

local retry = math.max(rolling_retry(30, 1), rolling_retry(3600, 3))
if redis.call('ZCOUNT', key, day_start, now) >= 5 then retry = math.max(retry, midnight) end
if retry > 0 then return {retry, retry - now} end

local sequence = redis.call('INCR', seq)
redis.call('ZADD', key, now, tostring(now) .. '-' .. tostring(sequence))
local ttl = math.max(3600, midnight - now) + 1
redis.call('EXPIRE', key, ttl)
redis.call('EXPIRE', seq, ttl)
return {0, 0}
"""


async def require_token(
    _: object = Depends(require_admin),
) -> None:
    return None


def _resolve_upload_path(path: str, allowed_dir: Path) -> Path:
    base_dir = allowed_dir.resolve()
    candidate = Path(path)
    full_path = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    if base_dir not in full_path.parents and full_path != base_dir:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not full_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return full_path


def process_uploaded_image(raw_bytes: bytes, filename_base: str) -> tuple[Path, Path, Path]:
    try:
        with ExitStack() as cleanup, PilImage.open(BytesIO(raw_bytes), formats=list(IMAGE_FORMATS)) as image:
            if image.width * image.height > MAX_DECODED_IMAGE_PIXELS:
                raise PilImage.DecompressionBombError
            suffix, _ = IMAGE_FORMATS[image.format]
            original = ORIGINAL_DIR / f"{filename_base}-original{suffix}"
            cleanup.callback(original.unlink, missing_ok=True)
            original.write_bytes(raw_bytes)
            jpg = thumb = original
            if image.format == "GIF":
                image.verify()
            else:
                jpg = JPG_DIR / f"{filename_base}-jpg.jpg"
                thumb = THUMB_DIR / f"{filename_base}-thumb.jpg"
                cleanup.callback(jpg.unlink, missing_ok=True)
                cleanup.callback(thumb.unlink, missing_ok=True)
                with image.convert("RGB") as rgb:
                    rgb.save(jpg, format="JPEG", quality=90, optimize=True)
                    rgb.thumbnail((300, 300))
                    rgb.save(thumb, format="JPEG", quality=70, optimize=True)
            cleanup.pop_all()
            return original, jpg, thumb
    except PilImage.DecompressionBombError as exc:
        raise HTTPException(413, {"error": "image_too_large", "max_pixels": MAX_DECODED_IMAGE_PIXELS}) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise HTTPException(400, {"error": "unsupported_image_format"}) from exc


def image_response(path: Path) -> FileResponse:
    # Previously stored originals may have a filename supplied by the uploader.
    try:
        with PilImage.open(path, formats=list(IMAGE_FORMATS)) as image:
            _, media_type = IMAGE_FORMATS[image.format]
    except (OSError, ValueError, SyntaxError, PilImage.DecompressionBombError) as exc:
        raise HTTPException(404, "Not found") from exc
    return FileResponse(path, media_type=media_type, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cache-Control": "private, no-store",
    })


async def _enforce_upload_rate_limit(redis: AsyncScriptRedis, address: str) -> None:
    key = f"rate_limit:box:uploads:{address}"
    retry_at, wait = await redis.eval(RATE_LIMIT_SCRIPT, 2, key, key + ":seq")
    if retry_at:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"retry_at": retry_at},
            headers={"Retry-After": str(wait)},
        )


@router.post("/uploads", response_model=UploadResponse)
async def upload_message(
    request: Request,
    message: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    files: list[UploadFile] | None = File(default=None),
    session: AsyncSession = Depends(get_db_session),
    redis: AsyncScriptRedis = Depends(get_rate_limit_redis),
) -> UploadResponse:
    ip_value = client_ip(request)
    await _enforce_upload_rate_limit(redis, ip_value)
    message_value = message.strip() if message else ""
    tag_value = tag.strip() if tag else ""
    missing_fields: list[str] = []
    if not message_value:
        missing_fields.append("message")
    if not tag_value:
        missing_fields.append("tag")
    if missing_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"missing_fields": missing_fields},
        )
    if files and len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"error": "too_many_files", "max_files": MAX_UPLOAD_FILES},
        )
    if len(tag_value) > 255 or len(message_value.encode("utf-8")) > 65535:
        raise HTTPException(422, "留言或标签过长")
    prepared_files: list[bytes] = []
    for upload in files or []:
        raw_bytes = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(raw_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"error": "file_too_large", "max_bytes": MAX_UPLOAD_BYTES},
            )
        prepared_files.append(raw_bytes)
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    JPG_DIR.mkdir(parents=True, exist_ok=True)

    with ExitStack() as file_cleanup:
        message_row = Message(ip_address=ip_value, message_text=message_value, tag=tag_value)
        session.add(message_row)
        await session.flush()

        image_ids: list[int] = []
        for raw_bytes in prepared_files:
            file_id = uuid4().hex
            filename_base = f"{message_row.message_id}-{file_id}"
            original_path, jpg_path, thumb_path = await asyncio.to_thread(
                process_uploaded_image,
                raw_bytes,
                filename_base,
            )
            for path in {original_path, jpg_path, thumb_path}:
                file_cleanup.callback(path.unlink, missing_ok=True)

            image_row = Image(
                message_id=message_row.message_id,
                image_path=str(original_path),
                thumb_path=str(thumb_path),
                jpg_path=str(jpg_path),
            )
            session.add(image_row)
            await session.flush()
            image_ids.append(image_row.image_id)

        await session.commit()
        _ = file_cleanup.pop_all()
    return UploadResponse(message_id=message_row.message_id, image_ids=image_ids, code=0)


@router.get("/image/original")
def download_original(path: str, _: None = Depends(require_token)) -> FileResponse:
    file_path = _resolve_upload_path(path, ORIGINAL_DIR)
    return image_response(file_path)


@router.get("/image/thumb")
def download_thumbnail(path: str, _: None = Depends(require_token)) -> FileResponse:
    try:
        file_path = _resolve_upload_path(path, THUMB_DIR)
    except HTTPException:
        if Path(path).suffix.lower() != ".gif":
            raise
        file_path = _resolve_upload_path(path, ORIGINAL_DIR)
    return image_response(file_path)


@router.get("/image/jpg")
def download_jpg(path: str, _: None = Depends(require_token)) -> FileResponse:
    try:
        file_path = _resolve_upload_path(path, JPG_DIR)
    except HTTPException:
        if Path(path).suffix.lower() != ".gif":
            raise
        file_path = _resolve_upload_path(path, ORIGINAL_DIR)
    return image_response(file_path)


@router.get("/pending", response_model=MessageListResponse)
async def list_pending(
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> MessageListResponse:
    result = await session.execute(
        select(Message)
        .options(selectinload(Message.images))
        .where(Message.status == "pending")
        .order_by(Message.created_at.desc())
    )
    messages = result.scalars().all()
    return MessageListResponse.from_messages(list(messages))


@router.get("/approved", response_model=MessageListResponse)
async def list_approved(
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> MessageListResponse:
    result = await session.execute(
        select(Message)
        .options(selectinload(Message.images))
        .where(Message.status == "approved")
        .order_by(Message.created_at.desc())
    )
    messages = result.scalars().all()
    return MessageListResponse.from_messages(list(messages))


@router.post("/approve")
async def approve_all(
    payload: TagFilterRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict[str, int | str]:
    stmt = update(Message).where(Message.status == "pending")
    if payload and payload.tag:
        stmt = stmt.where(Message.tag == payload.tag)
    result = await session.execute(stmt.values(status="approved"))
    await session.commit()
    if payload and payload.tag:
        return {"code": 0, "message": f"标签{payload.tag}下{result.rowcount or 0}条消息已过审"}
    return {"code": 0, "message": f"{result.rowcount or 0}条消息已过审"}


@router.post("/delete")
async def delete_message(
    payload: DeleteRequest,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict[str, int | str]:
    result = await session.execute(
        update(Message)
        .where(Message.message_id == payload.id)
        .values(status="delete")
    )
    await session.commit()
    if not result.rowcount:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return {"code": 0, "message": f"id{payload.id}已删除"}


@router.post("/archived")
async def archived_all(
    payload: TagFilterRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict[str, int | str]:
    stmt = update(Message).where(Message.status == "approved")
    if payload and payload.tag:
        stmt = stmt.where(Message.tag == payload.tag)
    result = await session.execute(stmt.values(status="archived"))
    await session.commit()
    if payload and payload.tag:
        return {"code": 0, "message": f"标签{payload.tag}下{result.rowcount or 0}条消息已归档"}
    return {"code": 0, "message": f"{result.rowcount or 0}条消息已归档"}

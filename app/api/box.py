from __future__ import annotations

from pathlib import Path
import time
from uuid import uuid4
import ipaddress

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image as PilImage
from redis.asyncio import Redis
from sqlalchemy import update, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.redis import get_redis_client
from app.db.session import get_db_session
from app.deps.auth import get_bearer_token
from app.models.image import Image
from app.models.message import Message
from app.schemas.box import DeleteRequest, MessageListResponse, UploadResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/box")

UPLOAD_ROOT = Path("uploads")
ORIGINAL_DIR = UPLOAD_ROOT / "original"
THUMB_DIR = UPLOAD_ROOT / "thumbs"
JPG_DIR = UPLOAD_ROOT / "jpg"

RATE_LIMIT_WINDOWS = (
    (30, 1),
    (60 * 60, 3),
    (60 * 60 * 24, 5),
)

RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window1 = tonumber(ARGV[2])
local limit1 = tonumber(ARGV[3])
local window2 = tonumber(ARGV[4])
local limit2 = tonumber(ARGV[5])
local window3 = tonumber(ARGV[6])
local limit3 = tonumber(ARGV[7])

local max_window = math.max(window1, window2, window3)
redis.call("ZREMRANGEBYSCORE", key, 0, now - max_window)

local function check_window(window, limit)
    local count = redis.call("ZCOUNT", key, now - window + 1, now)
    if tonumber(count) >= limit then
        local oldest = redis.call("ZRANGEBYSCORE", key, now - window + 1, now, "LIMIT", 0, 1, "WITHSCORES")
        if oldest[2] then
            return tonumber(oldest[2]) + window
        end
    end
    return nil
end

local retry_at = check_window(window1, limit1)
if retry_at then
    return {0, retry_at}
end
retry_at = check_window(window2, limit2)
if retry_at then
    return {0, retry_at}
end
retry_at = check_window(window3, limit3)
if retry_at then
    return {0, retry_at}
end

local seq_key = key .. ":seq"
local seq = redis.call("INCR", seq_key)
redis.call("ZADD", key, now, tostring(now) .. "-" .. tostring(seq))
redis.call("EXPIRE", key, max_window + 1)
redis.call("EXPIRE", seq_key, max_window + 1)
return {1, 0}
"""


async def require_token(
    token: str = Depends(get_bearer_token),
    redis: Redis = Depends(get_redis_client),
) -> None:
    service = AuthService(redis)
    username = await service.get_username_by_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def _resolve_upload_path(path: str, allowed_dir: Path) -> Path:
    base_dir = allowed_dir.resolve()
    candidate = Path(path)
    full_path = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    if base_dir not in full_path.parents and full_path != base_dir:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not full_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return full_path


async def _enforce_upload_rate_limit(redis: Redis, client_ip: str) -> None:
    if client_ip == "0.0.0.0":
        return
    key = f"rate_limit:box:uploads:{client_ip}"
    now = int(time.time())
    result = await redis.eval(
        RATE_LIMIT_SCRIPT,
        1,
        key,
        now,
        RATE_LIMIT_WINDOWS[0][0],
        RATE_LIMIT_WINDOWS[0][1],
        RATE_LIMIT_WINDOWS[1][0],
        RATE_LIMIT_WINDOWS[1][1],
        RATE_LIMIT_WINDOWS[2][0],
        RATE_LIMIT_WINDOWS[2][1],
    )
    if isinstance(result, (list, tuple)) and result and int(result[0]) == 0:
        retry_at = int(result[1])
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"retry_at": retry_at},
        )


@router.post("/uploads", response_model=UploadResponse)
async def upload_message(
    request: Request,
    message: str | None = Form(default=None),
    tag: str | None = Form(default=None),
    files: list[UploadFile] | None = File(default=None),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis_client),
) -> UploadResponse:
    client_ip = request.headers.get("Eo-Connecting-Ip")
    if not client_ip:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else "0.0.0.0"
    await _enforce_upload_rate_limit(redis, client_ip)
    missing_fields: list[str] = []
    if message is None or not message.strip():
        missing_fields.append("message")
    if tag is None or not tag.strip():
        missing_fields.append("tag")
    if missing_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"missing_fields": missing_fields},
        )
    ip_value = str(ipaddress.ip_address(client_ip))

    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    JPG_DIR.mkdir(parents=True, exist_ok=True)

    message_row = Message(ip_address=ip_value, message_text=message.strip(), tag=tag.strip())
    session.add(message_row)
    await session.flush()

    image_ids: list[int] = []
    if files:
        for upload in files:
            raw_bytes = await upload.read()
            file_suffix = Path(upload.filename or "").suffix.lower() or ".bin"
            file_id = uuid4().hex

            filename_base = f"{message_row.message_id}-{file_id}"
            original_path = ORIGINAL_DIR / f"{filename_base}-original{file_suffix}"
            original_path.write_bytes(raw_bytes)

            with PilImage.open(original_path) as img:
                rgb_image = img.convert("RGB")

                jpg_path = JPG_DIR / f"{filename_base}-jpg.jpg"
                rgb_image.save(jpg_path, format="JPEG", quality=90, optimize=True)

                thumb_image = rgb_image.copy()
                thumb_image.thumbnail((300, 300))
                thumb_path = THUMB_DIR / f"{filename_base}-thumb.jpg"
                thumb_image.save(thumb_path, format="JPEG", quality=70, optimize=True)

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
    return UploadResponse(message_id=message_row.message_id, image_ids=image_ids, code=0)


@router.get("/image/original")
async def download_original(path: str, _: None = Depends(require_token)) -> FileResponse:
    file_path = _resolve_upload_path(path, ORIGINAL_DIR)
    return FileResponse(file_path)


@router.get("/image/thumb")
async def download_thumbnail(path: str, _: None = Depends(require_token)) -> FileResponse:
    file_path = _resolve_upload_path(path, THUMB_DIR)
    return FileResponse(file_path)


@router.get("/image/jpg")
async def download_jpg(path: str, _: None = Depends(require_token)) -> FileResponse:
    file_path = _resolve_upload_path(path, JPG_DIR)
    return FileResponse(file_path)


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
    return MessageListResponse.from_messages(messages)


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
    return MessageListResponse.from_messages(messages)


@router.post("/approve")
async def approve_all(
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict:
    result = await session.execute(
        update(Message)
        .where(Message.status == "pending")
        .values(status="approved")
    )
    await session.commit()
    return {"code": 0, "message": f"{result.rowcount or 0}条消息已过审"}


@router.post("/delete")
async def delete_message(
    payload: DeleteRequest,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict:
    result = await session.execute(
        update(Message)
        .where(Message.message_id == payload.id)
        .values(status="deleted")
    )
    await session.commit()
    if not result.rowcount:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return {"code": 0, "message": f"id{payload.id}已删除"}


@router.post("/archived")
async def archived_all(
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict:
    result = await session.execute(
        update(Message)
        .where(Message.status == "approved")
        .values(status="archived")
    )
    await session.commit()
    return {"code": 0, "message": f"{result.rowcount or 0}条消息已归档"}

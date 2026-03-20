from __future__ import annotations

from pathlib import Path
import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image as PilImage, UnidentifiedImageError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis_client
from app.db.session import get_db_session
from app.deps.auth import get_bearer_token
from app.models.captain_gift_archive import CaptainGiftArchive
from app.schemas.captaingift import CaptainGiftListResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/captaingift")

CAPTAIN_GIFT_DIR = Path("uploads") / "captaingift"


async def require_token(
    token: str = Depends(get_bearer_token),
    redis: Redis = Depends(get_redis_client),
) -> None:
    service = AuthService(redis)
    username = await service.get_username_by_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def _process_captaingift_image(raw_bytes: bytes, target_path: Path) -> None:
    temp_path = target_path.with_suffix(".upload")

    try:
        temp_path.write_bytes(raw_bytes)
        with PilImage.open(temp_path) as img:
            rgb_image = img.convert("RGB")
            rgb_image.save(target_path, format="JPEG", quality=90, optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        target_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image",
        ) from exc
    finally:
        temp_path.unlink(missing_ok=True)


def _resolve_gift_path(path: str) -> Path:
    base_dir = CAPTAIN_GIFT_DIR.resolve()
    candidate = Path(path)
    full_path = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    if base_dir not in full_path.parents and full_path != base_dir:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not full_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return full_path


@router.get("", response_model=CaptainGiftListResponse)
async def list_captaingifts(
    session: AsyncSession = Depends(get_db_session),
) -> CaptainGiftListResponse:
    result = await session.execute(
        select(CaptainGiftArchive).order_by(CaptainGiftArchive.gift_month.desc())
    )
    rows = result.scalars().all()
    return CaptainGiftListResponse.from_rows(rows)


@router.get("/image")
async def download_captaingift_image(
    month: str,
    session: AsyncSession = Depends(get_db_session),
) -> FileResponse:
    result = await session.execute(
        select(CaptainGiftArchive).where(CaptainGiftArchive.gift_month == month)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    file_path = _resolve_gift_path(row.image_path)
    return FileResponse(file_path)


@router.post("/add")
async def upload_captaingift(
    month: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict:
    if not month.isdigit() or len(month) != 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid month")

    CAPTAIN_GIFT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{month}.jpg"
    target_path = CAPTAIN_GIFT_DIR / filename

    raw_bytes = await file.read()
    await asyncio.to_thread(_process_captaingift_image, raw_bytes, target_path)

    result = await session.execute(
        select(CaptainGiftArchive).where(CaptainGiftArchive.gift_month == month)
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.image_path = str(target_path)
    else:
        session.add(CaptainGiftArchive(gift_month=month, image_path=str(target_path)))
    await session.commit()
    return {"code": 0, "message": f"{month}已上传"}

from __future__ import annotations

import logging
from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image as PilImage, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.deps.auth import require_admin
from app.models.captain_gift_archive import CaptainGiftArchive
from app.schemas.captaingift import CaptainGiftDeleteRequest, CaptainGiftListResponse
from app.services.captain_gift_archive import CAPTAIN_GIFT_DIR, build_captaingift_path

router = APIRouter(prefix="/captaingift")
logger = logging.getLogger(__name__)


async def require_token(
    _: object = Depends(require_admin),
) -> None:
    return None


def _process_captaingift_image(raw_bytes: bytes, target_path: Path) -> None:
    temp_path = target_path.with_suffix(".upload")

    try:
        _ = temp_path.write_bytes(raw_bytes)
        with PilImage.open(temp_path) as img:
            rgb_image = img.convert("RGB")
            rgb_image.save(target_path, format="JPEG", quality=90, optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        _ = target_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image",
        ) from exc
    finally:
        _ = temp_path.unlink(missing_ok=True)


def _resolve_gift_path(path: str, *, require_file: bool = True) -> Path:
    base_dir = CAPTAIN_GIFT_DIR.resolve()
    candidate = Path(path)
    full_path = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    if base_dir not in full_path.parents and full_path != base_dir:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if require_file and not full_path.is_file():
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
    path: str,
) -> FileResponse:
    file_path = _resolve_gift_path(path)
    return FileResponse(file_path)


@router.post("/add")
async def upload_captaingift(
    month: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict[str, int | str]:
    if not month.isdigit() or len(month) != 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid month")

    raw_bytes = await file.read()
    CAPTAIN_GIFT_DIR.mkdir(parents=True, exist_ok=True)
    target_path = build_captaingift_path(month, raw_bytes)
    await to_thread.run_sync(_process_captaingift_image, raw_bytes, target_path)

    result = await session.execute(
        select(CaptainGiftArchive).where(CaptainGiftArchive.gift_month == month)
    )
    existing = result.scalar_one_or_none()
    previous_path: Path | None = None
    if existing:
        previous_path = _resolve_gift_path(existing.image_path, require_file=False)
        existing.image_path = str(target_path)
    else:
        session.add(CaptainGiftArchive(gift_month=month, image_path=str(target_path)))
    await session.commit()

    if previous_path is not None and previous_path != target_path:
        try:
            _ = previous_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("旧舰礼图片清理失败 path=%s error=%s", previous_path, exc)

    return {"code": 0, "message": f"{month}已上传"}


@router.post("/delete")
async def delete_captaingift(
    payload: CaptainGiftDeleteRequest,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict[str, int | str]:
    month = payload.month
    if not month.isdigit() or len(month) != 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid month")

    result = await session.execute(
        select(CaptainGiftArchive).where(CaptainGiftArchive.gift_month == month)
    )
    existing = result.scalar_one_or_none()
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    file_path = _resolve_gift_path(existing.image_path, require_file=False)
    await session.delete(existing)
    await session.commit()

    try:
        _ = file_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("舰礼图片删除失败 path=%s error=%s", file_path, exc)

    return {"code": 0, "message": f"{month}已删除"}

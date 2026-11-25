from __future__ import annotations

import logging
import re
from io import BytesIO
from tempfile import NamedTemporaryFile
from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image as PilImage, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.hot_cache import public_cache, public_fence
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
    temp_path = None
    try:
        with PilImage.open(BytesIO(raw_bytes)) as img:
            if PilImage.MAX_IMAGE_PIXELS and img.width * img.height > PilImage.MAX_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail="Image dimensions too large")
            with NamedTemporaryFile(dir=target_path.parent, suffix=".tmp", delete=False) as output:
                temp_path = Path(output.name)
                with img.convert("RGB") as rgb:
                    rgb.save(output, format="JPEG", quality=90, optimize=True)
        temp_path.replace(target_path)
    except PilImage.DecompressionBombError as exc:
        raise HTTPException(status_code=413, detail="Image dimensions too large") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid image") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


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
@public_cache("archives")
async def list_captaingifts(
    session: AsyncSession = Depends(get_db_session),
) -> CaptainGiftListResponse:
    latest = select(func.max(CaptainGiftArchive.id)).group_by(CaptainGiftArchive.gift_month)
    result = await session.execute(
        select(CaptainGiftArchive)
        .where(CaptainGiftArchive.id.in_(latest))
        .order_by(CaptainGiftArchive.gift_month.desc())
    )
    rows = result.scalars().all()
    return CaptainGiftListResponse.from_rows(rows)


@router.get("/image")
async def download_captaingift_image(
    path: str,
) -> FileResponse:
    file_path = _resolve_gift_path(path)
    if file_path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise HTTPException(404, "Not found")
    return FileResponse(file_path, media_type="image/jpeg", headers={"X-Content-Type-Options": "nosniff"})


@router.post("/add")
async def upload_captaingift(
    month: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict[str, int | str]:
    if not re.fullmatch(r"[0-9]{4}(?:0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=400, detail="Invalid month")

    raw_bytes = await file.read()
    CAPTAIN_GIFT_DIR.mkdir(parents=True, exist_ok=True)
    target_path = build_captaingift_path(month, raw_bytes)
    await public_fence(session, "archives", write=True)
    await to_thread.run_sync(_process_captaingift_image, raw_bytes, target_path)

    result = await session.execute(
        select(CaptainGiftArchive).where(CaptainGiftArchive.gift_month == month)
    )
    rows = result.scalars().all()
    previous_paths = {_resolve_gift_path(row.image_path, require_file=False) for row in rows}
    if rows:
        for row in rows:
            row.image_path = str(target_path)
    else:
        session.add(CaptainGiftArchive(gift_month=month, image_path=str(target_path)))
    await session.commit()
    await _remove_replaced_images(session, previous_paths - {target_path.resolve()})

    return {"code": 0, "message": f"{month}已上传"}


async def _remove_replaced_images(session: AsyncSession, paths: set[Path]) -> None:
    if not paths:
        return
    # A later upload may have reselected a hash, or a legacy row may share its file.
    await public_fence(session, "archives", write=True)
    current = await session.scalars(select(CaptainGiftArchive.image_path))
    paths.difference_update(Path(saved).resolve() for saved in current)
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("舰礼图片清理失败 path=%s error=%s", path, exc)


@router.post("/delete")
async def delete_captaingift(
    payload: CaptainGiftDeleteRequest,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict[str, int | str]:
    await public_fence(session, "archives", write=True)
    month = payload.month
    if not re.fullmatch(r"[0-9]{4}(?:0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid month")

    result = await session.execute(
        select(CaptainGiftArchive).where(CaptainGiftArchive.gift_month == month)
    )
    rows = result.scalars().all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    paths = {_resolve_gift_path(row.image_path, require_file=False) for row in rows}
    for row in rows:
        await session.delete(row)
    await session.commit()
    await _remove_replaced_images(session, paths)

    return {"code": 0, "message": f"{month}已删除"}

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.captain_gift_archive import CaptainGiftArchive

logger = logging.getLogger(__name__)

CAPTAIN_GIFT_DIR = Path("uploads") / "captaingift"
_LEGACY_FILENAME = re.compile(r"^(?P<month>\d{6})\.jpg$")


def build_captaingift_path(month: str, raw_bytes: bytes) -> Path:
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return CAPTAIN_GIFT_DIR / f"{month}_{digest}.jpg"


def _legacy_month(path: Path) -> str | None:
    match = _LEGACY_FILENAME.fullmatch(path.name)
    return match.group("month") if match else None


def _migrate_file(legacy_path: Path, month: str) -> Path:
    raw_bytes = legacy_path.read_bytes()
    target_path = build_captaingift_path(month, raw_bytes)
    if target_path.exists():
        _ = legacy_path.unlink()
    else:
        _ = legacy_path.replace(target_path)
    return target_path


async def migrate_legacy_captaingift_images() -> None:
    if not CAPTAIN_GIFT_DIR.is_dir():
        return

    async with async_session_factory() as session:
        result = await session.execute(select(CaptainGiftArchive))
        rows = list(result.scalars().all())
        rows_by_month: dict[str, list[CaptainGiftArchive]] = {}
        for row in rows:
            rows_by_month.setdefault(row.gift_month, []).append(row)

        changed = False
        for legacy_path in CAPTAIN_GIFT_DIR.glob("*.jpg"):
            month = _legacy_month(legacy_path)
            if month is None:
                continue

            try:
                target_path = _migrate_file(legacy_path, month)
            except OSError as exc:
                logger.warning("舰礼图片迁移失败 path=%s error=%s", legacy_path, exc)
                continue

            stored_path = str(target_path)
            month_rows = rows_by_month.get(month, [])
            if month_rows:
                for row in month_rows:
                    if row.image_path != stored_path:
                        row.image_path = stored_path
                        changed = True
            else:
                session.add(CaptainGiftArchive(gift_month=month, image_path=stored_path))
                rows_by_month[month] = []
                changed = True

        for row in rows:
            if _legacy_month(Path(row.image_path)) != row.gift_month:
                continue

            candidates = sorted(CAPTAIN_GIFT_DIR.glob(f"{row.gift_month}_*.jpg"))
            if len(candidates) == 1 and row.image_path != str(candidates[0]):
                row.image_path = str(candidates[0])
                changed = True

        if changed:
            await session.commit()
            logger.info("舰礼历史图片迁移完成，更新记录数=%d", len(rows))

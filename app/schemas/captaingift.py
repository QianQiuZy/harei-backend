from collections.abc import Sequence

from pydantic import BaseModel

from app.models.captain_gift_archive import CaptainGiftArchive


class CaptainGiftDeleteRequest(BaseModel):
    month: str


class CaptainGiftItem(BaseModel):
    month: str
    path: str


class CaptainGiftListResponse(BaseModel):
    code: int = 0
    items: list[CaptainGiftItem]

    @staticmethod
    def from_rows(rows: Sequence[CaptainGiftArchive]) -> "CaptainGiftListResponse":
        items = [
            CaptainGiftItem(month=row.gift_month, path=row.image_path)
            for row in rows
        ]
        return CaptainGiftListResponse(items=items)

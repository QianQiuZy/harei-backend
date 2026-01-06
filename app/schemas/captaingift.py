from pydantic import BaseModel


class CaptainGiftItem(BaseModel):
    month: str
    path: str


class CaptainGiftListResponse(BaseModel):
    code: int = 0
    items: list[CaptainGiftItem]

    @staticmethod
    def from_rows(rows: list) -> "CaptainGiftListResponse":
        items = [
            CaptainGiftItem(month=row.gift_month, path=row.image_path)
            for row in rows
        ]
        return CaptainGiftListResponse(items=items)

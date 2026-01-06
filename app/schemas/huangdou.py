from pydantic import BaseModel


class GiftRankingItem(BaseModel):
    uid: str
    name: str | None
    count: int


class GiftRankingResponse(GiftRankingItem):
    code: int = 0


class GiftRankingListResponse(BaseModel):
    code: int = 0
    items: list[GiftRankingItem]

from datetime import datetime

from pydantic import BaseModel


class CaptainItem(BaseModel):
    uid: str
    name: str | None
    level: str
    count: int
    red_packet: bool
    joined_at: datetime


class CaptainListResponse(BaseModel):
    code: int = 0
    items: list[CaptainItem]

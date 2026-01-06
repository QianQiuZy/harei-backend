from datetime import datetime

from pydantic import BaseModel


class TagItem(BaseModel):
    tag_id: int
    tag_name: str
    status: str
    expires_at: datetime | None
    created_at: datetime


class TagListResponse(BaseModel):
    code: int = 0
    items: list[TagItem]


class TagNameResponse(BaseModel):
    code: int = 0
    items: list[str]


class TagCreateRequest(BaseModel):
    tag_name: str


class TagUpdateRequest(BaseModel):
    tag_name: str

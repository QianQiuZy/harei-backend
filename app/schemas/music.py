from pydantic import BaseModel


class MusicItem(BaseModel):
    music_id: int
    title: str
    artist: str
    type: str | None
    language: str | None
    note: str | None


class MusicListResponse(BaseModel):
    code: int = 0
    items: list[MusicItem]

from pydantic import BaseModel


class DownloadItem(BaseModel):
    download_id: int
    description: str
    path: str


class DownloadListResponse(BaseModel):
    code: int = 0
    items: list[DownloadItem]


class DownloadAddResponse(BaseModel):
    code: int = 0
    download_id: int
    path: str

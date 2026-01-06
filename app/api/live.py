from __future__ import annotations

from fastapi import APIRouter

from app.services.bili_captain_listener import live_status_snapshot

router = APIRouter(prefix="/live", tags=["live"])


@router.get("/status")
async def get_live_status():
    """
    直播状态快照（内存态）。
    注意：如果 uvicorn 启用了多 worker，该状态为“每个进程各自一份”。
    """
    return live_status_snapshot()

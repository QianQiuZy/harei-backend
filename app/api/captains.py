from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis_client
from app.db.session import get_db_session
from app.deps.auth import get_bearer_token
from app.models.captain import Captain
from app.schemas.captains import CaptainItem, CaptainListResponse
from app.services.auth_service import AuthService

router = APIRouter()


async def require_token(
    token: str = Depends(get_bearer_token),
    redis: Redis = Depends(get_redis_client),
) -> None:
    service = AuthService(redis)
    username = await service.get_username_by_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


@router.get("/captains", response_model=CaptainListResponse)
async def list_captains(
    month: str | None = Query(default=None),
    uid: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> CaptainListResponse:
    current_month = datetime.utcnow().strftime("%Y%m")
    target_month = month or current_month

    query = select(Captain)
    if uid:
        query = query.where(Captain.user_uid == uid)
    else:
        query = query.where(Captain.joined_month == target_month)

    result = await session.execute(query.order_by(Captain.joined_at.asc()))
    rows = result.scalars().all()

    items = [
        CaptainItem(
            uid=row.user_uid,
            name=row.username,
            level=row.level,
            count=row.ship_count,
            red_packet=row.is_red_packet,
            joined_at=row.joined_at,
        )
        for row in rows
    ]
    return CaptainListResponse(items=items)

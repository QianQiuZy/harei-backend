from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis_client
from app.db.session import get_db_session
from app.deps.auth import get_bearer_token
from app.models.tag import Tag
from app.schemas.tag import TagCreateRequest, TagListResponse, TagNameResponse, TagUpdateRequest
from app.services.auth_service import AuthService

router = APIRouter(prefix="/tag")


async def require_token(
    token: str = Depends(get_bearer_token),
    redis: Redis = Depends(get_redis_client),
) -> None:
    service = AuthService(redis)
    username = await service.get_username_by_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


@router.get("/active", response_model=TagNameResponse)
async def list_active(session: AsyncSession = Depends(get_db_session)) -> TagNameResponse:
    result = await session.execute(select(Tag).where(Tag.status == "approved"))
    rows = result.scalars().all()
    return TagNameResponse(items=[row.tag_name for row in rows])


@router.post("/add")
async def add_tag(
    payload: TagCreateRequest,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict:
    result = await session.execute(select(Tag).where(Tag.tag_name == payload.tag_name))
    row = result.scalar_one_or_none()
    if row:
        row.status = "approved"
    else:
        row = Tag(tag_name=payload.tag_name, status="approved")
        session.add(row)
    await session.commit()
    return {"code": 0, "message": "ok"}


@router.get("/all", response_model=TagListResponse)
async def list_all(
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> TagListResponse:
    result = await session.execute(select(Tag).order_by(Tag.created_at.desc()))
    rows = result.scalars().all()
    items = [
        {
            "tag_id": row.tag_id,
            "tag_name": row.tag_name,
            "status": row.status,
            "expires_at": row.expires_at,
            "created_at": row.created_at,
        }
        for row in rows
    ]
    return TagListResponse(items=items)


@router.post("/archived")
async def archived_tag(
    payload: TagUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
    _: None = Depends(require_token),
) -> dict:
    result = await session.execute(
        update(Tag)
        .where(Tag.tag_name == payload.tag_name)
        .values(status="archived")
    )
    await session.commit()
    if not result.rowcount:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return {"code": 0, "message": "ok"}

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.gift_ranking import GiftRanking
from app.schemas.huangdou import GiftRankingItem, GiftRankingListResponse, GiftRankingResponse

router = APIRouter(prefix="/huangdou")


@router.get("/rank", response_model=GiftRankingListResponse)
async def list_rank(session: AsyncSession = Depends(get_db_session)) -> GiftRankingListResponse:
    result = await session.execute(
        select(GiftRanking).order_by(desc(GiftRanking.gift_count)).limit(20)
    )
    rows = result.scalars().all()
    items = [
        GiftRankingItem(uid=row.user_uid, name=row.username, count=row.gift_count)
        for row in rows
    ]
    return GiftRankingListResponse(items=items)


@router.get("/uid", response_model=GiftRankingResponse)
async def get_by_uid(
    uid: str = Query(...),
    session: AsyncSession = Depends(get_db_session),
) -> GiftRankingResponse:
    result = await session.execute(select(GiftRanking).where(GiftRanking.user_uid == uid))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return GiftRankingResponse(uid=row.user_uid, name=row.username, count=row.gift_count, code=0)

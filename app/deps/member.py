import secrets
from typing import Annotated

import anyio
from fastapi import Depends, HTTPException, Request, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import get_rate_limit_redis, get_redis_client, get_session_redis
from app.db.session import get_db_session
from app.models.member_gifts import Member
from app.services.member_cache import Account, read_account
from app.services.member_gifts import advance, lock_system, now_cn
from app.services.member_security import PREFIX, SESSION_COOKIE, digest

gift_request_slots = anyio.Semaphore(min(3, get_settings().mysql_pool_size))


async def gift_transaction(db: Annotated[AsyncSession, Depends(get_db_session)]):
    # MySQL serializes these transactions on one row. Queue locally before
    # checkout so a burst of writes cannot occupy the entire connection pool.
    with anyio.move_on_after(get_settings().mysql_pool_timeout) as wait:
        await gift_request_slots.acquire()
    if wait.cancelled_caught:
        raise HTTPException(
            503, "舰礼操作繁忙，请稍后重试", headers={"Retry-After": "2"}
        )
    try:
        await lock_system(db)
        await advance(db, now_cn())
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    finally:
        gift_request_slots.release()


GiftDB = Annotated[AsyncSession, Depends(gift_transaction, scope="function")]
ReadDB = Annotated[AsyncSession, Depends(get_db_session)]
RedisDB = Annotated[Redis, Depends(get_redis_client)]
SessionDB = Annotated[Redis, Depends(get_session_redis)]
LimitDB = Annotated[Redis, Depends(get_rate_limit_redis)]


def session_key(token):
    return PREFIX + "session:" + digest("session", token)


async def start_session(
    request: Request, response: Response, redis: Redis, member: Member
):
    token = secrets.token_urlsafe(32)
    ttl = get_settings().token_ttl_seconds
    await redis.set(session_key(token), f"{member.id}:{member.session_version}", ex=ttl)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=ttl,
        secure=(
            not get_settings().local_mode
            or request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}
        ),
    )


async def session_identity(request: Request, redis: SessionDB):
    token = request.cookies.get(SESSION_COOKIE, "")
    saved = await redis.get(session_key(token)) if token else None
    if saved:
        owner, version = saved.split(":")
        return int(owner), int(version)
    raise HTTPException(401, "请登录个人账号")


SessionIdentity = Annotated[tuple[int, int], Depends(session_identity)]


async def current_member(identity: SessionIdentity, db: GiftDB) -> Member:
    owner, version = identity
    member = await db.get(Member, owner)
    if member and member.email and member.session_version == version:
        return member
    raise HTTPException(401, "请登录个人账号")


async def read_member(identity: SessionIdentity, db: ReadDB) -> Account:
    owner, version = identity
    member = await read_account(db, owner=owner)
    if member and member.session_version == version:
        return member
    raise HTTPException(401, "请登录个人账号")


ReadingMember = Annotated[Account, Depends(read_member)]
SignedIn = Annotated[Member, Depends(current_member)]


def invalidate(member):
    member.session_version += 1

import asyncio
from ipaddress import IPv6Address, ip_address
from uuid import uuid4
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.redis import get_rate_limit_redis

# Redis supplies one clock for all workers and makes the midnight boundary atomic
# with the reservation. Beijing uses UTC+8 without daylight saving time.
BEIJING_WINDOW = """
local now = tonumber(redis.call('TIME')[1])
local day_start = now - ((now + 8 * 3600) % 86400)
local midnight = day_start + 86400
"""


def _canonical_ip(raw: str) -> str | None:
    try:
        value = ip_address(raw.strip())
    except ValueError:
        return None
    if value.is_unspecified or (isinstance(value, IPv6Address) and value.scope_id):
        return None
    if isinstance(value, IPv6Address) and value.ipv4_mapped:
        return str(value.ipv4_mapped)
    return str(value)


def client_ip(request: Request) -> str:
    peer = _canonical_ip(request.client.host) if request.client else None
    settings = get_settings()
    trusted = {_canonical_ip(host) for host in settings.trusted_proxy_hosts_list}
    if not peer or peer not in trusted:
        return peer or "0.0.0.0"
    # A general reverse proxy may simply pass through the incoming CDN header.
    # Opt in only peers that overwrite it with an authenticated EdgeOne client IP.
    if peer in {
        _canonical_ip(host) for host in settings.trusted_edgeone_proxy_hosts.split(",")
    }:
        edge = _canonical_ip(request.headers.get("eo-connecting-ip", ""))
        if edge:
            return edge
    for hop in reversed(
        ",".join(request.headers.getlist("x-forwarded-for")).split(",")
    ):
        address = _canonical_ip(hop)
        if address is None:
            return peer
        if address not in trusted:
            return address
    return peer


ADMIN_LOGIN_WINDOW = (
    BEIJING_WINDOW
    + """
local day, count = unpack(redis.call('HMGET', KEYS[1], 'day', 'count'))
local used = tonumber(day) == day_start and tonumber(count) or 0
if ARGV[1] == 'reserve' then
    if used >= 10 then return midnight - now end
    redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
    if used + redis.call('ZCARD', KEYS[2]) >= 10 then return -1 end
    redis.call('ZADD', KEYS[2], now + 60, ARGV[2])
    redis.call('EXPIREAT', KEYS[2], math.min(midnight, now + 61))
    return 0
end
redis.call('ZREM', KEYS[2], ARGV[2])
if used >= 10 then return midnight - now end
if ARGV[1] == 'failure' then
    if tonumber(day) ~= day_start then redis.call('HSET', KEYS[1], 'day', day_start, 'count', 0) end
    redis.call('HINCRBY', KEYS[1], 'count', 1)
    redis.call('EXPIREAT', KEYS[1], midnight)
end
return 0
"""
)


async def admin_login_budget(
    request: Request, limits: Annotated[Redis, Depends(get_rate_limit_redis)]
):
    key = "rate_limit:admin-login:" + client_ip(request)

    pending = key + ":pending"
    ticket = uuid4().hex

    async def attempt(action):
        wait = await limits.eval(ADMIN_LOGIN_WINDOW, 2, key, pending, action, ticket)
        if wait > 0:
            raise HTTPException(
                412,
                "今日后台登录失败次数已达上限，北京时间零点后可重试",
                headers={"Retry-After": str(wait)},
            )
        return wait

    # Pending checks reserve the remaining attempts. Only a completed failure spends one.
    try:
        async with asyncio.timeout(5):
            while await attempt("reserve") < 0:
                await asyncio.sleep(0.05)
    except TimeoutError as exc:
        raise HTTPException(503, "后台登录繁忙，请稍后重试", headers={"Retry-After": "1"}) from exc

    finished = False

    async def finish(*, failure=False):
        nonlocal finished
        await attempt("failure" if failure else "success")
        finished = True

    try:
        yield finish
    finally:
        if not finished:
            try:
                await limits.zrem(pending, ticket)
            except RedisError:
                pass  # A crashed or cancelled verifier's lease expires on its own.

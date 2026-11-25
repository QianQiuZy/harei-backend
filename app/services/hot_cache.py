"""Redis read-through caches backed by MySQL. Each reader fills under the same
InnoDB fence that writers hold while retiring the old value before commit.
"""

import hashlib
import hmac
import inspect as pyinspect
import json
import logging
import secrets
from dataclasses import dataclass, field
from functools import wraps

import anyio
from fastapi import HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis import PUBLIC_DB, get_cache_redis
from app.models.member_gifts import Binding, GiftMonth, GiftOrder, Member

TTL = 30 * 24 * 3600
MAX_PUBLIC_VARIANTS = 128
MAX_PUBLIC_BYTES = 512 * 1024
PUBLIC_HANDLERS = {}
_fill_locks = tuple(anyio.Lock() for _ in range(64))


def fill_lock(key):
    return _fill_locks[hash(key) % len(_fill_locks)]


logger = logging.getLogger(__name__)


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Store:
    redis: Redis
    prefix: str

    def key(self, name):
        return self.prefix + name

    async def generation(self, group):
        key = self.key(group + ":version")
        version = await self.redis.get(key)
        if version is None:
            await self.redis.set(key, secrets.token_hex(16), nx=True, ex=TTL * 2)
            version = await self.redis.get(key)
        return version

    async def rotate(self, group):
        version = secrets.token_hex(16)
        await self.redis.set(self.key(group + ":version"), version, ex=TTL * 2)
        return version


async def store(db):
    redis = await get_cache_redis(db)
    # A Redis restart/restore must never revive stale account or address data from AOF/RDB.
    run_id = (await redis.info("server"))["run_id"]
    settings = get_settings()
    database = pack([settings.mysql_host, settings.mysql_port, settings.mysql_database])
    installation = hmac.digest(
        settings.app_secret_key.encode(), database.encode(), "sha256"
    ).hex()[:24]
    return Store(redis, f"harei:hot:v1:{installation}:{run_id}:")


async def public_fence(db, group, *, write=False):
    statement = (
        "SELECT name FROM hot_cache_fences WHERE name=:name FOR UPDATE"
        if write else "SELECT name FROM hot_cache_fences WHERE name=:name LOCK IN SHARE MODE"
    )
    row = await db.scalar(text(statement), {"name": group})
    if row is None:
        raise RuntimeError("Missing hot cache schema migration")


# Limit the cardinality of arbitrary search/pagination combinations independently
# of Redis's global eviction policy, which also protects sessions and rate limits.
_PUBLISH_PUBLIC = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('ZADD', KEYS[3], redis.call('TIME')[1], KEYS[2])
redis.call('EXPIRE', KEYS[3], ARGV[3])
redis.call('SET', KEYS[4], ARGV[5], 'EX', ARGV[3])
if redis.call('ZCARD', KEYS[3]) > tonumber(ARGV[4]) then
    local oldest = redis.call('ZPOPMIN', KEYS[3], 1)
    redis.call('DEL', oldest[1])
end
return 1
"""


async def read_public(cache, version_key, key, request, response):
    version, raw = await cache.redis.mget(version_key, key)
    if not version or not raw or raw.startswith("!"):
        return None
    try:
        saved_version, etag, value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if saved_version != version:
        return None
    if etag:
        response.headers["ETag"] = etag
        if request and request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
    return value


def public_cache(group):
    """Cache allowlisted, unauthenticated JSON handlers and their ETags only."""

    def decorate(handler):
        name = handler.__name__
        PUBLIC_HANDLERS.setdefault(group, {})[name] = handler

        @wraps(handler)
        async def cached(*args, **kwargs):
            session = kwargs["session"]
            params = {
                k: v
                for k, v in kwargs.items()
                if k not in ("request", "response", "session")
            }
            field = hashlib.sha256(
                pack([handler.__name__, sorted(params.items())]).encode()
            ).hexdigest()
            try:
                cache = await store(PUBLIC_DB)
                key = cache.key(group + ":" + field)
                version_key = cache.key(group + ":version")
                hit = await read_public(
                    cache,
                    version_key,
                    key,
                    kwargs.get("request"),
                    kwargs.get("response"),
                )
                if hit is not None:
                    return hit
            except RedisError:
                return await handler(*args, **kwargs)

            async with fill_lock(key):
                try:
                    hit = await read_public(
                        cache,
                        version_key,
                        key,
                        kwargs.get("request"),
                        kwargs.get("response"),
                    )
                    if hit is not None:
                        return hit
                    await public_fence(session, group)
                    version = await cache.generation(group)
                except RedisError:
                    return await handler(*args, **kwargs)

                result = await handler(*args, **kwargs)
                if isinstance(result, Response):
                    return result
                response = kwargs.get("response")
                etag = response.headers.get("ETag") if response is not None else None
                raw = pack([version, etag, jsonable_encoder(result)])
                if cacheable_size(name, raw):
                    try:
                        await cache.redis.eval(
                            _PUBLISH_PUBLIC,
                            4,
                            version_key,
                            key,
                            cache.key(group + ":items"),
                            cache.key(group + ":warm:" + name),
                            version,
                            raw,
                            TTL,
                            MAX_PUBLIC_VARIANTS,
                            pack([field, params]),
                        )
                    except RedisError:
                        pass  # A cache fill is optional. The SQL result is complete.
                return result

        # Some route modules use postponed annotations from their own globals.
        cached.__signature__ = pyinspect.signature(handler, eval_str=True)
        return cached

    return decorate


def cacheable_size(name, raw):
    limit = 4 * 1024 * 1024 if name == "export_music" else MAX_PUBLIC_BYTES
    return len(raw.encode()) <= limit


async def refresh_public(db, group):
    cache = await store(PUBLIC_DB)
    await public_fence(db, group, write=True)
    version = await cache.rotate(group)
    values = {}
    # Refresh the last hot list/detail/export of each type synchronously. The
    # generation retires every other search or page without replaying 128 queries.
    for name, handler in PUBLIC_HANDLERS.get(group, {}).items():
        saved = await cache.redis.get(cache.key(group + ":warm:" + name))
        if not saved:
            continue
        field, params = json.loads(saved)
        key = group + ":" + field
        if not await cache.redis.exists(cache.key(key)):
            continue
        response = Response()
        kwargs = {**params, "session": db}
        signature = pyinspect.signature(handler).parameters
        if "request" in signature:
            kwargs["request"] = Request({"type": "http", "headers": []})
        if "response" in signature:
            kwargs["response"] = response
        try:
            result = await handler(**kwargs)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            values[key] = ""
            continue
        raw = pack([version, response.headers.get("etag"), jsonable_encoder(result)])
        values[key] = raw if cacheable_size(name, raw) else ""
        await cache.redis.expire(cache.key(group + ":warm:" + name), TTL)
    return await reserve(cache, values)


PUBLIC_TABLES = {
    "music_catalog_revision": "music",
    "tags": "tags",
    "downloads": "downloads",
    "captain_gift_archives": "archives",
}


@dataclass
class Changes:
    members: set = field(default_factory=set)
    owners: set = field(default_factory=set)
    aliases: set = field(default_factory=set)
    view_owners: set = field(default_factory=set)
    order_uids: set = field(default_factory=set)
    months: bool = False
    public: set = field(default_factory=set)


class TrackedSession(Session):
    pass


@event.listens_for(TrackedSession, "before_flush")
def collect_changes(db, flush_context, instances):
    changes = db.info.setdefault("hot_changes", Changes())
    for obj in db.new | db.dirty | db.deleted:
        state = inspect(obj)
        if obj in db.dirty and not db.is_modified(obj, include_collections=False):
            continue
        if isinstance(obj, Member):
            if obj in db.dirty and not any(
                state.attrs[k].history.has_changes()
                for k in ("email", "session_version", "password_hash")
            ):
                changes.view_owners.add(obj.id)
                continue
            changes.members.add(obj)
            changes.aliases.update(v for v in state.attrs.email.history.deleted if v)
        elif isinstance(obj, Binding):
            changes.view_owners.add(obj.member_id)
            if obj in db.dirty and not any(
                state.attrs[k].history.has_changes() for k in ("member_id", "username")
            ):
                continue
            changes.owners.add(obj.member_id)
            changes.aliases.add(obj.uid)
            changes.owners.update(state.attrs.member_id.history.deleted)
        elif isinstance(obj, GiftOrder):
            changes.order_uids.add(obj.uid)
            if obj.owner_id:
                changes.view_owners.add(obj.owner_id)
        elif isinstance(obj, GiftMonth):
            if (
                obj in db.new
                or obj in db.deleted
                or any(
                    state.attrs[k].history.has_changes()
                    for k in ("settled", "deadline")
                )
            ):
                changes.months = True
        elif group := PUBLIC_TABLES.get(obj.__tablename__):
            changes.public.add(group)


@event.listens_for(TrackedSession, "do_orm_execute")
def collect_bulk_changes(execution):
    if execution.is_update or execution.is_delete or execution.is_insert:
        table = getattr(execution.statement, "table", None)
        group = PUBLIC_TABLES.get(getattr(table, "name", None))
        if group:
            execution.session.info.setdefault("hot_changes", Changes()).public.add(
                group
            )


@event.listens_for(TrackedSession, "after_rollback")
def discard_changes(db):
    db.info.pop("hot_changes", None)


async def prepare_commit(db):
    await db.flush()
    changes = db.info.get("hot_changes")
    if not changes:
        return []
    publications = []
    if (
        changes.members
        or changes.owners
        or changes.view_owners
        or changes.aliases
        or changes.order_uids
        or changes.months
    ):
        from app.services.member_cache import prepare_members

        publications = await prepare_members(db, changes)
    if changes.public:
        for group in sorted(changes.public):
            publications.append(await refresh_public(db, group))
    return publications


# Publish only over our own pre-commit marker. A subsequent commit or a cold read
# can win the race. A crashed writer leaves an ordinary cache miss.
_PUBLISH = """
for i, key in ipairs(KEYS) do
    if redis.call('GET', key) == ARGV[1] then
        if ARGV[i+2] == '' then redis.call('DEL', key)
        else redis.call('SET', key, ARGV[i+2], 'EX', ARGV[2]) end
    end
end
"""


async def reserve(cache, values):
    if not values:
        return None
    token = "!" + secrets.token_hex(16)
    keys = [cache.key(k) for k in values]
    async with cache.redis.pipeline(transaction=True) as pipe:
        for key in keys:
            pipe.set(key, token, ex=TTL)
        await pipe.execute()
    return cache, keys, token, list(values.values())


async def publish(records):
    for record in records:
        if record:
            cache, keys, token, values = record
            try:
                await cache.redis.eval(_PUBLISH, len(keys), *keys, token, TTL, *values)
            except RedisError:
                logger.warning("Hot cache refill deferred until next read")

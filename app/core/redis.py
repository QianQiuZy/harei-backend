from threading import Lock

import anyio
from redis import ConnectionPool as SyncPool
from redis import Redis as SyncRedis
from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.connection import parse_url
from redis.connection import parse_url as parse_sync_url
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.config import get_settings

MAX_REDIS_CONNECT_RETRIES = 3
REDIS_CONNECT_RETRY_BACKOFF_SECONDS = 0.5
RATE_LIMIT_DB = 2
ACCOUNT_DB = 3
ADDRESS_DB = 4
MEMBER_SESSION_DB = 5
PUBLIC_DB = 6
_clients: dict[int | None, Redis] = {}
_locks = {
    db: anyio.Lock()
    for db in (
        None,
        RATE_LIMIT_DB,
        ACCOUNT_DB,
        ADDRESS_DB,
        MEMBER_SESSION_DB,
        PUBLIC_DB,
    )
}


async def move_keys(source: Redis, destination: int, patterns: tuple[str, ...]) -> None:
    # MOVE preserves the remaining TTL and never overwrites a destination key.
    if int(source.connection_pool.connection_kwargs.get("db", 0)) == destination:
        return
    for pattern in patterns:
        cursor = 0
        while True:
            cursor, keys = await source.scan(cursor, match=pattern, count=250)
            if keys:
                async with source.pipeline(transaction=True) as pipe:
                    for key in keys:
                        if destination == MEMBER_SESSION_DB:
                            # Discard duplicate legacy sessions so a later rollout cannot resurrect a logout.
                            pipe.eval(
                                "if redis.call('MOVE', KEYS[1], ARGV[1]) == 0 then redis.call('DEL', KEYS[1]) end",
                                1,
                                key,
                                destination,
                            )
                        else:
                            pipe.move(key, destination)
                    await pipe.execute()
            if not cursor:
                break


async def _ping(client: Redis) -> None:
    for attempt in range(1, MAX_REDIS_CONNECT_RETRIES + 1):
        try:
            await client.ping()
            return
        except (RedisConnectionError, RedisTimeoutError):
            if attempt == MAX_REDIS_CONNECT_RETRIES:
                raise
            await anyio.sleep(REDIS_CONNECT_RETRY_BACKOFF_SECONDS * attempt)


async def _get_client(db: int | None) -> Redis:
    cached = _clients.get(db)
    if cached is not None:
        return cached
    async with _locks[db]:
        if db in _clients:
            return _clients[db]
        options = parse_url(get_settings().redis_url)
        options["decode_responses"] = True
        options.setdefault("socket_connect_timeout", 2)
        options.setdefault("socket_timeout", 3)
        if db is not None:
            options["db"] = db
        client = Redis.from_pool(ConnectionPool(**options))
        try:
            if db not in (ACCOUNT_DB, ADDRESS_DB, PUBLIC_DB):
                await _ping(client)
            if db == RATE_LIMIT_DB:
                await move_keys(
                    await get_redis_client(),
                    db,
                    ("rate_limit:box:uploads:*", "harei:members:limit:*"),
                )
            elif db == MEMBER_SESSION_DB:
                await move_keys(
                    await get_redis_client(), db, ("harei:members:session:*",)
                )
        except BaseException:
            await client.aclose()
            raise
        _clients[db] = client
        return client


async def get_redis_client() -> Redis:
    return await _get_client(None)


async def get_rate_limit_redis() -> Redis:
    return await _get_client(RATE_LIMIT_DB)


async def get_session_redis() -> Redis:
    return await _get_client(MEMBER_SESSION_DB)


async def get_cache_redis(db: int) -> Redis:
    if db not in (ACCOUNT_DB, ADDRESS_DB, PUBLIC_DB):
        raise ValueError("Unknown hot data database")
    return await _get_client(db)


_sync_rate_client: SyncRedis | None = None
_sync_rate_lock = Lock()


def get_sync_rate_limit_redis() -> SyncRedis:
    global _sync_rate_client
    with _sync_rate_lock:
        if _sync_rate_client is None:
            settings = get_settings()
            url = (
                settings.gift_mail_rate_redis_url.get_secret_value()
                or settings.redis_url
            )
            options = parse_sync_url(url)
            options["db"] = RATE_LIMIT_DB
            options.setdefault("socket_connect_timeout", 2)
            options.setdefault("socket_timeout", 3)
            _sync_rate_client = SyncRedis.from_pool(SyncPool(**options))
        return _sync_rate_client


async def close_redis_client() -> None:
    global _sync_rate_client
    with _sync_rate_lock:
        if _sync_rate_client is not None:
            _sync_rate_client.close()
            _sync_rate_client = None
    for client in _clients.values():
        await client.aclose()
    _clients.clear()

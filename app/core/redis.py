import asyncio

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.config import get_settings


MAX_REDIS_CONNECT_RETRIES = 3
REDIS_CONNECT_RETRY_BACKOFF_SECONDS = 0.5

_redis_client: Redis | None = None
_redis_lock = asyncio.Lock()


async def _create_redis_client() -> Redis:
    settings = get_settings()
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    await client.ping()
    return client


async def get_redis_client() -> Redis:
    global _redis_client

    if _redis_client is not None:
        try:
            await _redis_client.ping()
            return _redis_client
        except (RedisConnectionError, RedisTimeoutError):
            await _redis_client.close()
            _redis_client = None

    async with _redis_lock:
        if _redis_client is not None:
            return _redis_client

        for attempt in range(1, MAX_REDIS_CONNECT_RETRIES + 1):
            try:
                _redis_client = await _create_redis_client()
                return _redis_client
            except (RedisConnectionError, RedisTimeoutError):
                if attempt >= MAX_REDIS_CONNECT_RETRIES:
                    raise
                await asyncio.sleep(REDIS_CONNECT_RETRY_BACKOFF_SECONDS * attempt)

    raise RuntimeError("Redis client initialization failed unexpectedly.")

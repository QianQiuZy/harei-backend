import os
from collections.abc import AsyncIterator

from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import text

hasher = PasswordHasher()
os.environ.update(
    {
        "APP_SECRET_KEY": "test",
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3307",
        "MYSQL_USER": "root",
        "MYSQL_PASSWORD": "",
        "MYSQL_DATABASE": "harei_test",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "AUTH_USERNAME": "admin",
        "AUTH_PASSWORD_HASH": hasher.hash("admin-password"),
        "MUSIC_AUTH_USERNAME": "music",
        "MUSIC_AUTH_PASSWORD_HASH": hasher.hash("music-password"),
        "BILI_MONITOR_ENABLED": "false",
    }
)


@pytest_asyncio.fixture(autouse=True)
async def reset_catalog() -> AsyncIterator[None]:
    from app.core.redis import close_redis_client, get_redis_client
    from app.db.session import engine

    async def clear_catalog() -> None:
        async with engine.begin() as connection:
            _ = await connection.execute(text("DELETE FROM music_audit_events"))
            _ = await connection.execute(text("DELETE FROM song_performances"))
            _ = await connection.execute(text("DELETE FROM songs"))
            _ = await connection.execute(
                text("UPDATE music_catalog_revision SET revision = 0 WHERE id = 1")
            )

    await clear_catalog()
    redis = await get_redis_client()
    await redis.flushdb()
    yield
    await clear_catalog()
    await close_redis_client()
    await engine.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http_client:
        yield http_client


@pytest.fixture
def song_payload() -> dict[str, str | list[str]]:
    return {
        "title": "Test Song",
        "artist": "Test Artist",
        "artists": ["Test Artist"],
        "genre": "华语流行",
        "language": "中文",
        "work_type": "翻唱",
        "notes": "",
        "metadata_status": "complete",
    }

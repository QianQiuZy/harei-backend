import asyncio

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

DATABASE_URL = (
    "mysql+asyncmy://"
    f"{settings.mysql_user}:{settings.mysql_password}"
    f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"
    "?charset=utf8mb4"
)

engine: AsyncEngine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

MAX_DB_CONNECT_RETRIES = 3
DB_CONNECT_RETRY_BACKOFF_SECONDS = 0.5

async def get_db_session() -> AsyncSession:
    for attempt in range(1, MAX_DB_CONNECT_RETRIES + 1):
        try:
            async with async_session_factory() as session:
                await session.execute(text("SELECT 1"))
                yield session
                return
        except (OperationalError, DBAPIError):
            if attempt >= MAX_DB_CONNECT_RETRIES:
                raise
            await engine.dispose()
            await asyncio.sleep(DB_CONNECT_RETRY_BACKOFF_SECONDS * attempt)

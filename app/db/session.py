from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.services.hot_cache import TrackedSession, prepare_commit, publish

settings = get_settings()
DATABASE_URL = URL.create(
    "mysql+asyncmy",
    username=settings.mysql_user,
    password=settings.mysql_password,
    host=settings.mysql_host,
    port=settings.mysql_port,
    database=settings.mysql_database,
    query={"charset": "utf8mb4"},
)
engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    hide_parameters=True,
    isolation_level="READ COMMITTED",
    pool_size=settings.mysql_pool_size,
    max_overflow=settings.mysql_max_overflow,
    pool_timeout=settings.mysql_pool_timeout,
    pool_recycle=settings.mysql_pool_recycle,
)


class ApplicationSession(AsyncSession):
    sync_session_class = TrackedSession

    async def commit(self):
        pending = await prepare_commit(self)
        await super().commit()
        self.info.pop("hot_changes", None)
        await publish(pending)


class SessionFactory(async_sessionmaker):
    @asynccontextmanager
    async def begin(self):
        async with self() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise


async_session_factory = SessionFactory(
    engine, class_=ApplicationSession, expire_on_commit=False
)


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    # Constructing a session is lazy. Hot reads never check out a MySQL connection.
    async with async_session_factory() as session:
        yield session

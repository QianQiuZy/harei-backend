import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from sqlalchemy import Connection, func, inspect, select, table, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import get_db_session


MIGRATION_DATABASE = f"harei_migration_test_{os.getpid()}"
MYSQL_SERVER_URL = "mysql+asyncmy://root:@127.0.0.1:3307/?charset=utf8mb4"
MYSQL_MIGRATION_URL = (
    f"mysql+asyncmy://root:@127.0.0.1:3307/{MIGRATION_DATABASE}?charset=utf8mb4"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def get_table_names(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


async def reset_migration_database(*, stamped: bool, has_songs: bool) -> None:
    server_engine = create_async_engine(MYSQL_SERVER_URL)
    async with server_engine.begin() as connection:
        _ = await connection.execute(
            text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE}")
        )
        _ = await connection.execute(
            text(
                f"CREATE DATABASE {MIGRATION_DATABASE} DEFAULT CHARACTER SET "
                + "utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
    await server_engine.dispose()

    database_engine = create_async_engine(MYSQL_MIGRATION_URL)
    async with database_engine.begin() as connection:
        if has_songs:
            _ = await connection.execute(
                text(
                    "CREATE TABLE songs (song_id INT AUTO_INCREMENT PRIMARY KEY, "
                    + "source_key VARCHAR(80) NOT NULL UNIQUE) ENGINE=InnoDB"
                )
            )
            _ = await connection.execute(
                text("INSERT INTO songs (source_key) VALUES ('existing-song')")
            )
        _ = await connection.execute(
            text(
                "CREATE TABLE music (id INT AUTO_INCREMENT PRIMARY KEY, "
                + "title VARCHAR(255) NOT NULL) ENGINE=InnoDB"
            )
        )
        _ = await connection.execute(
            text("INSERT INTO music (title) VALUES ('legacy-song')")
        )
        if stamped:
            _ = await connection.execute(
                text(
                    "CREATE TABLE alembic_version "
                    + "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
            _ = await connection.execute(
                text(
                    "INSERT INTO alembic_version (version_num) "
                    + "VALUES ('20260725_music')"
                )
            )
    await database_engine.dispose()


async def drop_migration_database() -> None:
    server_engine = create_async_engine(MYSQL_SERVER_URL)
    async with server_engine.begin() as connection:
        _ = await connection.execute(
            text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE}")
        )
    await server_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stamped", "has_songs"),
    [(False, True), (True, True), (False, False)],
)
async def test_alembic_upgrade_repairs_schema_without_losing_existing_data(
    stamped: bool,
    has_songs: bool,
) -> None:
    environment = {
        **os.environ,
        "APP_SECRET_KEY": "migration-test",
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3307",
        "MYSQL_USER": "root",
        "MYSQL_PASSWORD": "",
        "MYSQL_DATABASE": MIGRATION_DATABASE,
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "AUTH_USERNAME": "admin",
        "AUTH_PASSWORD_HASH": "unused",
    }

    try:
        # Given an existing catalog database that predates the audit table.
        await reset_migration_database(stamped=stamped, has_songs=has_songs)

        # When the documented production upgrade command runs.
        process = await anyio.run_process(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
        )

        # Then the audit table exists and existing catalog data remains.
        assert process.returncode == 0, process.stderr.decode()
        database_engine = create_async_engine(MYSQL_MIGRATION_URL)
        async with database_engine.connect() as connection:
            table_names = await connection.run_sync(get_table_names)
            song_count = await connection.scalar(
                select(func.count()).select_from(table("songs"))
            )
        await database_engine.dispose()
        assert "music_audit_events" in table_names
        assert "music" in table_names
        assert song_count == int(has_songs)
    finally:
        await drop_migration_database()


@pytest.mark.asyncio
async def test_db_dependency_preserves_request_database_error() -> None:
    # Given a request-scoped database dependency and a handler database failure.
    request_error = ProgrammingError(
        "INSERT INTO music_audit_events",
        {},
        RuntimeError("table does not exist"),
    )
    dependency_context = asynccontextmanager(get_db_session)

    # When FastAPI throws the handler failure back into the dependency generator.
    with pytest.raises(ProgrammingError) as raised:
        async with dependency_context():
            raise request_error

    # Then the original error escapes instead of a second-yield RuntimeError.
    assert raised.value is request_error

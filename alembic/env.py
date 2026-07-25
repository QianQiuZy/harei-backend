import asyncio
import sys
from pathlib import Path
from logging.config import fileConfig
from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.base import Base

config = context.config
if config.config_file_name: fileConfig(config.config_file_name)
settings = get_settings()
config.set_main_option("sqlalchemy.url", f"mysql+asyncmy://{settings.mysql_user}:{settings.mysql_password}@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}?charset=utf8mb4")
target_metadata = Base.metadata

def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction(): context.run_migrations()

def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, compare_server_default=True)
    with context.begin_transaction(): context.run_migrations()

async def run_async_migrations() -> None:
    engine = async_engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection: await connection.run_sync(do_run_migrations)
    await engine.dispose()

if context.is_offline_mode(): run_migrations_offline()
else: asyncio.run(run_async_migrations())

"""Use local Settings without writing database credentials into migration files."""

import asyncio
from pathlib import Path

from alembic import context
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from travel_agent.durable_storage import Base
from travel_tools.config import Settings

url = make_url(Settings().database_url)
if url.drivername in {"postgres", "postgresql"}:
    url = url.set(drivername="postgresql+asyncpg")
if url.drivername == "sqlite":
    url = url.set(drivername="sqlite+aiosqlite")
if url.drivername == "sqlite+aiosqlite" and url.database not in {None, ":memory:"}:
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())

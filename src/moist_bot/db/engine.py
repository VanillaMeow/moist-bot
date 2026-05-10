from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from moist_bot.constants import DB_PATH

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


DATABASE_URL = f'sqlite+aiosqlite:///{DB_PATH}'


def _configure_sqlite_connection(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA synchronous=NORMAL')
    cursor.close()


def configure_sqlite_engine(engine: AsyncEngine) -> None:
    if engine.sync_engine.dialect.name != 'sqlite':
        return

    event.listen(engine.sync_engine, 'connect', _configure_sqlite_connection)


def create_engine() -> AsyncEngine:
    engine = create_async_engine(DATABASE_URL)
    configure_sqlite_engine(engine)
    return engine


def create_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_context(
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    async with session_maker() as session:
        yield session

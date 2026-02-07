from __future__ import annotations

__all__ = ('create_pool',)

import asqlite


async def setup_db_tables(conn: asqlite.Connection) -> None:
    async with conn:
        query = """--sql
            CREATE TABLE
                IF NOT EXISTS pfg_param_cache (
                    user_id INTEGER PRIMARY KEY,
                    damages TEXT DEFAULT '[]', -- JSON array
                    ranges TEXT DEFAULT '[]', -- JSON array
                    multiplier REAL DEFAULT 1.0,
                    rpm REAL
                )
        """
        await conn.execute(query)


async def create_pool() -> asqlite.Pool:
    pool = await asqlite.create_pool('moist.db')
    async with pool.acquire() as conn:
        await setup_db_tables(conn)

    return pool

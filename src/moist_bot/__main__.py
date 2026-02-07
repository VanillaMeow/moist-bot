from __future__ import annotations

import asyncio

from .bot import MoistBot
from .db import create_pool
from .utils.setup_logging import setup_logging


def main() -> None:
    asyncio.run(_main())


async def _main() -> None:
    await run_bot()


async def run_bot() -> None:
    with setup_logging():
        async with MoistBot() as client:
            client.pool = await create_pool()
            await client.start()


if __name__ == '__main__':
    main()

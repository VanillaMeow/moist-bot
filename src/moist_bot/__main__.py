from __future__ import annotations

import asyncio
from contextlib import suppress

from .bot import MoistBot
from .utils.setup_logging import setup_logging


async def run_bot() -> None:
    with setup_logging(), suppress(KeyboardInterrupt, asyncio.CancelledError):
        async with MoistBot() as client:
            await client.start()


async def _main() -> None:
    await run_bot()


def main() -> None:
    asyncio.run(_main())


if __name__ == '__main__':
    main()

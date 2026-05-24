from __future__ import annotations

import asyncio

from moist_bot.bot import MoistBot
from moist_bot.bot_fleabot import FleaBot
from moist_bot.settings import settings
from moist_bot.utils.logger import setup_logging

# uvloop is Posix only
try:
    import uvloop
except ImportError:
    async_driver = asyncio
else:
    async_driver = uvloop


async def run_bot() -> None:
    bot_cls = FleaBot if settings.use_fleabot else MoistBot
    async with bot_cls() as client:
        await client.start()


async def _main() -> None:
    with setup_logging():
        try:
            await run_bot()
        except KeyboardInterrupt, asyncio.CancelledError:
            pass


def main() -> None:
    async_driver.run(_main())


if __name__ == '__main__':
    main()

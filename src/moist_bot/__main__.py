from __future__ import annotations

import asyncio

from moist_bot.bot import MoistBot
from moist_bot.settings import settings
from moist_bot.utils.logger import setup_logging

# uvloop is Posix only
try:
    import uvloop
except ImportError:
    async_driver = asyncio
else:
    async_driver = uvloop


# Select the bot class based on settings
bot_cls = MoistBot
if settings.use_fleabot:
    from moist_bot.bot_fleabot import FleaBot
    bot_cls = FleaBot



async def run_bot() -> None:
    async with bot_cls() as bot:
        await bot.start()


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

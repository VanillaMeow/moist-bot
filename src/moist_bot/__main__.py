from __future__ import annotations

import asyncio
import gc
import os
import sys

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
if settings.is_fleabot:
    from moist_bot.bot_fleabot import FleaBot

    bot_cls = FleaBot


async def run_bot() -> bool:
    async with bot_cls() as bot:
        await bot.start()
    return bot.restart_requested


async def _main() -> bool:
    with setup_logging():
        try:
            return await run_bot()
        except KeyboardInterrupt, asyncio.CancelledError:
            return False


def main() -> None:
    restart_requested = async_driver.run(_main())
    if restart_requested:
        # Release cyclic resources after all async shutdown work has finished
        gc.collect()
        os.execv(sys.executable, [sys.executable, *sys.argv])  # ruff: ignore[start-process-with-no-shell]


if __name__ == '__main__':
    main()

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp
import asqlite
import discord
import discord.utils
from discord.ext import commands

from .config import TOKEN
from .constants import COGS_FOLDER_PATH, DATETIME_NEVER, ROOT_PACKAGE
from .utils.context import Context

if TYPE_CHECKING:
    from discord import Interaction, Message


log = logging.getLogger('discord.' + __name__)


extras = ('water ', 'Water ')
sep = '-' * 12


def _get_prefix(bot: MoistBot, message: Message) -> list[str]:
    return commands.when_mentioned_or(*extras)(bot, message)


class MoistBot(commands.Bot):
    executor: ProcessPoolExecutor
    session: aiohttp.ClientSession
    pool: asqlite.Pool

    reminder = None

    def __init__(self):
        allowed_mentions = discord.AllowedMentions(
            everyone=False, roles=False, users=True, replied_user=True
        )
        intents = discord.Intents(
            emojis_and_stickers=True,
            message_content=True,
            reactions=True,
            webhooks=True,
            messages=True,
            invites=True,
            members=True,
            guilds=True,
        )
        super().__init__(
            allowed_mentions=allowed_mentions,
            help_attrs={'hidden': True},
            command_prefix=_get_prefix,
            enable_debug_events=True,
            case_insensitive=True,
            intents=intents,
        )

        self.started_at: datetime = DATETIME_NEVER
        self.cooldowns: dict[int, datetime] = {}
        self.synced: bool = True

    async def load_cogs(self) -> None:
        cogs = COGS_FOLDER_PATH.name

        for file in COGS_FOLDER_PATH.iterdir():
            # Ignore non-python files or __init__.py
            if file.suffix != '.py' or file.stem == '__init__':
                continue

            try:
                await self.load_extension(f'.{cogs}.{file.stem}', package=ROOT_PACKAGE)
            except commands.ExtensionError:
                log.exception(f'Failed to load extension {file}\n')

    async def setup_hook(self) -> None:
        self.executor = ProcessPoolExecutor(max_workers=4)
        self.session = aiohttp.ClientSession()
        await asyncio.create_task(self.load_cogs())

    async def get_context(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, origin: Message | Interaction, /, *, cls: type[Context] = Context
    ) -> Context:
        return await super().get_context(origin, cls=cls)

    async def process_commands(self, message: Message, /) -> None:
        if message.author.bot:
            return

        ctx: Context = await self.get_context(message)

        if ctx.command is not None:  # type: ignore[reportUnnecessaryComparison]
            log.debug(
                f"Command in guild '{ctx.guild}', by {ctx.author}, with command '{ctx.command}'\n"
            )

        await self.invoke(ctx)

    async def start(self, token: str = TOKEN, *, reconnect: bool = True) -> None:
        await super().start(token=token, reconnect=reconnect)

    async def close(self) -> None:
        await super().close()
        self.executor.shutdown()
        await self.session.close()
        await self.pool.close()
        log.info('Bot closed.')

    async def on_ready(self) -> None:
        guilds = len(self.guilds)
        await self.change_presence(
            status=discord.Status.idle,
            activity=discord.Game(f'with {guilds} moisturised servers'),
        )

        if self.started_at == DATETIME_NEVER:
            self.started_at = discord.utils.utcnow()
            log.info(f'\nLogged in as {self.user}\n{sep}\n')
        else:
            log.info(f'\nRelogged in after disconnect!\n{sep}\n')

        if not self.synced:
            await self.wait_until_ready()
            await self.tree.sync(guild=None)
            self.synced = True
            log.info('Application commands synced.')

    @property
    def config(self) -> Any:
        return __import__('config')

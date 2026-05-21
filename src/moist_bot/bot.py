from __future__ import annotations

import asyncio
import logging
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
import discord.utils
from discord import app_commands
from discord.ext import commands

from .constants import COGS_FOLDER_PATH, DATETIME_NEVER, ROOT_PACKAGE
from .db import create_engine, create_session_maker
from .models import BlocklistScope, BlocklistSource
from .services import BlocklistManager
from .settings import settings
from .utils.context import Context

if TYPE_CHECKING:
    from datetime import datetime

    from discord import Interaction, Message
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


log = logging.getLogger('discord.' + __name__)


BOT_PREFIXES = ('water ', 'Water ')


def _get_prefix(bot: MoistBot, message: Message) -> list[str]:
    return commands.when_mentioned_or(*BOT_PREFIXES)(bot, message)


class MoistCommandTree(app_commands.CommandTree['MoistBot']):
    """Application command tree that enforces blocklist checks globally."""

    async def interaction_check(
        self, interaction: discord.Interaction[MoistBot], /
    ) -> bool:
        """Reject blocklisted application command interactions before dispatch."""

        bot = self.client
        decision = await bot.blocklist.check_interaction(interaction)
        if decision is None:
            return True

        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(
                    ':no_entry_sign: You cannot use this bot here',
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        return False


class MoistBot(commands.Bot):
    executor: ProcessPoolExecutor
    session: aiohttp.ClientSession
    db_engine: AsyncEngine
    db_session_maker: async_sessionmaker[AsyncSession]
    blocklist: BlocklistManager
    spam_control: commands.CooldownMapping[Message]

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
            help_attrs={'hidden': True},  # type: ignore[]
            command_prefix=_get_prefix,
            enable_debug_events=True,
            case_insensitive=True,
            intents=intents,
            tree_cls=MoistCommandTree,
        )

        # Meta
        self.started_at: datetime = DATETIME_NEVER
        self.cooldowns: dict[tuple[int, str], datetime] = {}
        self.synced: bool = True

        # Database
        self.db_engine = create_engine()
        self.db_session_maker = create_session_maker(self.db_engine)

        # Services
        self.blocklist = BlocklistManager(self)
        self.spam_control = commands.CooldownMapping['Message'].from_cooldown(
            10,
            12.0,
            commands.BucketType.user,
        )
        self._auto_spam_count: Counter[int] = Counter()

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

        tasks = [
            asyncio.create_task(self.load_cogs()),
            asyncio.create_task(self.blocklist.load()),
        ]
        await asyncio.gather(*tasks)

    async def get_context(  # type: ignore[reportIncompatibleMethodOverride]
        self, origin: Message | Interaction, /, *, cls: type[Context] = Context
    ) -> Context:
        return await super().get_context(origin, cls=cls)

    async def can_run(  # type: ignore[reportIncompatibleMethodOverride]
        self, ctx: Context, /, *, call_once: bool = False
    ) -> bool:

        # No cooldown for bot owners
        command = ctx.command
        if not call_once and command is not None and await self.is_owner(ctx.author):
            command.reset_cooldown(ctx)

        return await super().can_run(ctx, call_once=call_once)

    async def process_commands(self, message: Message, /) -> None:
        """Resolve, filter, spam-check, and invoke a prefix command."""

        if message.author.bot:
            return

        ctx: Context = await self.get_context(message)
        if ctx.command is None:
            return

        log.debug(
            f"Command in guild '{ctx.guild}', by {ctx.author}, with command '{ctx.command}'\n"
        )

        if not await self.is_owner(ctx.author):
            # Blocklist checks happen before spam checks so blocked users do not
            # generate extra auto-blocklist noise while they are already blocked
            decision = await self.blocklist.check_context(ctx)
            if decision is not None:
                return

            if await self._handle_spamming(ctx):
                return

        await self.invoke(ctx)

    async def _handle_spamming(self, ctx: Context) -> bool:
        """Return whether this command attempt trips auto-blocklist spam control."""

        current = ctx.message.created_at.timestamp()
        bucket = self.spam_control.get_bucket(ctx.message, current)
        if bucket is None:
            return False

        retry_after = bucket.update_rate_limit(current)
        if retry_after is None:
            self._auto_spam_count.pop(ctx.author.id, None)
            return False

        self._auto_spam_count[ctx.author.id] += 1
        count = self._auto_spam_count[ctx.author.id]
        log.warning(
            f'Command spam attempt from {ctx.author} ({ctx.author.id}), '
            f'strike {count}/5. Retry after {retry_after:.1f}s.'
        )

        if count >= 5:
            self._auto_spam_count.pop(ctx.author.id, None)
            await self.blocklist.upsert_entry(
                scope=BlocklistScope.GLOBAL_USER,
                user_id=ctx.author.id,
                created_by_id=None,
                source=BlocklistSource.AUTO,
                reason='Automatic blocklist for command spam.',
            )
            await self.blocklist.log(
                f':no_entry_sign: Auto-blocklisted {ctx.author} '
                f'(`{ctx.author.id}`) for command spam.'
            )

        return True

    async def start(
        self, token: str = settings.token, *, reconnect: bool = True
    ) -> None:
        await super().start(token=token, reconnect=reconnect)

    async def close(self) -> None:
        await super().close()

        if 'executor' in self.__dict__:
            self.executor.shutdown()

        if 'session' in self.__dict__ and not self.session.closed:
            await self.session.close()

        await self.db_engine.dispose()

        log.info('Bot closed.')

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Leave guilds that are blocklisted for full membership denial."""

        if not self.blocklist.is_guild_blocklisted(guild.id):
            return

        log.warning(f'Leaving blocklisted guild {guild} ({guild.id}) after guild join.')
        try:
            await guild.leave()
        except discord.HTTPException:
            log.exception(f'Failed to leave blocklisted guild {guild} ({guild.id}).')

    async def on_ready(self) -> None:
        guilds = len(self.guilds)
        await self.change_presence(
            status=discord.Status.idle,
            activity=discord.Game(f'with {guilds} moisturised servers'),
        )

        sep = '-' * 12

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
        return __import__('settings')

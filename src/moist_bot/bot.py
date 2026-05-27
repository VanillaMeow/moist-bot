from __future__ import annotations

import asyncio
import logging
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING, cast, overload

import aiohttp
import discord
import discord.utils
from colorama import Fore
from discord.ext import commands

from .constants import COGS_FOLDER_PATH, DATETIME_NEVER, ROOT_PACKAGE
from .db import create_engine, create_session_maker
from .models import BlocklistScope, BlocklistSource
from .services import BlocklistManager, HoneypotManager
from .settings import settings
from .utils.context import Context, MoistCommandTree

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime
    from pathlib import Path
    from typing import Any, Unpack

    from discord import Message, app_commands
    from discord.ext.commands.bot import _BotOptions  # type: ignore[]
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from .utils.context import Interaction

    class BotOptions(_BotOptions, total=False):
        command_prefix: Callable[[commands.Bot, Message], list[str]]
        help_attrs: dict[str, Any]
        case_insensitive: bool
        intents: discord.Intents
        tree_cls: type[app_commands.CommandTree[Any]]


log = logging.getLogger('discord.' + __name__)


# Aliases
CYAN, RESET = Fore.CYAN, Fore.RESET


# Bot
COGS_PACKAGE_NAME = COGS_FOLDER_PATH.name
BOT_PREFIXES = ('water ', 'Water ', 'ww ', 'Ww ')
INTENTS = discord.Intents(
    emojis_and_stickers=True,
    message_content=True,
    reactions=True,
    webhooks=True,
    messages=True,
    invites=True,
    members=True,
    guilds=True,
)
ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=True
)


def _get_prefix(bot: commands.Bot, message: Message) -> list[str]:
    return commands.when_mentioned_or(*BOT_PREFIXES)(bot, message)


def is_extension_file(path: Path) -> bool:
    return path.suffix == '.py' and path.stem != '__init__'


def discover_extension_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            file.stem for file in COGS_FOLDER_PATH.iterdir() if is_extension_file(file)
        )
    )


def normalize_extension_name(name: str) -> str:
    normalized = name
    for prefix in (
        f'{ROOT_PACKAGE}.{COGS_PACKAGE_NAME}.',
        f'.{COGS_PACKAGE_NAME}.',
        f'{COGS_PACKAGE_NAME}.',
    ):
        normalized = normalized.removeprefix(prefix)

    return normalized.removesuffix('.py')


def extension_module_name(name: str) -> str:
    return f'.{COGS_PACKAGE_NAME}.{normalize_extension_name(name)}'


class MoistBot(commands.Bot):
    executor: ProcessPoolExecutor
    session: aiohttp.ClientSession
    db_engine: AsyncEngine
    db_session_maker: async_sessionmaker[AsyncSession]
    blocklist: BlocklistManager
    honeypot: HoneypotManager
    spam_control: commands.CooldownMapping[Message]

    reminder = None

    def __init__(
        self,
        *,
        startup_extensions: Iterable[str] | None = None,
        **kwargs: Unpack[BotOptions],
    ):
        kwargs.setdefault('allowed_mentions', ALLOWED_MENTIONS)
        kwargs.setdefault('help_attrs', {'hidden': True})
        kwargs.setdefault('command_prefix', _get_prefix)
        kwargs.setdefault('tree_cls', MoistCommandTree)
        kwargs.setdefault('enable_debug_events', True)
        kwargs.setdefault('case_insensitive', True)
        kwargs.setdefault('intents', INTENTS)

        super().__init__(**kwargs)

        self.startup_extensions: Iterable[str] | None = startup_extensions

        # Meta
        self.cooldowns: dict[tuple[int, str], datetime] = {}
        self.started_at: datetime = DATETIME_NEVER
        self.is_shutting_down: asyncio.Event = asyncio.Event()
        self.synced: bool = True

        # Database
        self.db_engine = create_engine()
        self.db_session_maker = create_session_maker(self.db_engine)

        # Services
        self.blocklist = BlocklistManager(self)
        self.honeypot = HoneypotManager(self)
        self.spam_control = commands.CooldownMapping['Message'].from_cooldown(
            rate=10,
            per=12.0,
            type=commands.BucketType.user,
        )
        self._auto_spam_count: Counter[int] = Counter()

    async def load_cogs(self) -> None:
        extension_names = (
            discover_extension_names()
            if self.startup_extensions is None
            else self.startup_extensions
        )

        for name in extension_names:
            try:
                await self.load_extension(name)
                log.info(f'Loaded extension {CYAN}{name}{RESET}.')
            except commands.ExtensionError:
                log.exception(f'Failed to load extension {CYAN}{name}{RESET}.')

    async def setup_hook(self) -> None:
        self.executor = ProcessPoolExecutor(max_workers=4)
        self.session = aiohttp.ClientSession()

        tasks = [
            asyncio.create_task(self.load_cogs()),
            asyncio.create_task(self.blocklist.load()),
            asyncio.create_task(self.honeypot.load()),
        ]
        await asyncio.gather(*tasks)

    async def start(
        self, token: str = settings.token, *, reconnect: bool = True
    ) -> None:
        await super().start(token=token, reconnect=reconnect)

    async def close(self) -> None:
        self.is_shutting_down.set()

        if hasattr(self, 'session') and not self.session.closed:
            await self.session.close()

        await self.db_engine.dispose()

        if hasattr(self, 'executor'):
            self.executor.shutdown()

        await super().close()

        log.info('Bot closed.')

    async def get_presence(self) -> discord.BaseActivity:
        guilds = len(self.guilds)
        return discord.Game(name=f'with {guilds} moisturized servers.')

    async def on_ready(self) -> None:
        await self.change_presence(activity=await self.get_presence())

        # Log connection
        if self.started_at == DATETIME_NEVER:
            self.started_at = discord.utils.utcnow()
            log.info(f'Connected as {self.user}')
        else:
            log.info('Reconnected after disconnect!')

        # Sync application commands
        if not self.synced:
            await self.wait_until_ready()
            await self.tree.sync(guild=None)
            self.synced = True
            log.info('Application commands synced.')

    async def get_context(  # type: ignore[reportIncompatibleMethodOverride]
        self, origin: Message | Interaction, /, *, cls: type[Context] = Context
    ) -> Context:
        return await super().get_context(origin, cls=cls)

    async def can_run(  # type: ignore[reportIncompatibleMethodOverride]
        self, ctx: Context, /, *, call_once: bool = False
    ) -> bool:
        if self.is_shutting_down.is_set():
            return False

        # No cooldown for bot owners
        command = cast('commands.Command[Any, ..., Any]', ctx.command)
        if not call_once and await self.is_owner(ctx.author):
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
            f"Command in guild '{ctx.guild}', by {ctx.author}, with command '{ctx.command}'"
        )

        if not await self.is_owner(ctx.author):
            # Blocklist checks happen before spam checks so blocked users do not
            # generate extra auto-blocklist noise while they are already blocked
            decision = await self.blocklist.check_context(ctx)
            if decision is not None:
                return

            if await self._handle_spamming(ctx):
                return

        if self.is_shutting_down.is_set():
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

    async def load_extension(
        self, name: str, *, package: str | None = ROOT_PACKAGE
    ) -> None:
        await super().load_extension(extension_module_name(name), package=package)

    async def reload_extension(
        self, name: str, *, package: str | None = ROOT_PACKAGE
    ) -> None:
        await super().reload_extension(extension_module_name(name), package=package)

    async def unload_extension(
        self, name: str, *, package: str | None = ROOT_PACKAGE
    ) -> None:
        await super().unload_extension(extension_module_name(name), package=package)

    @overload
    async def get_or_fetch_channel(
        self,
        channel_id: int,
        *,
        guild: discord.Guild,
    ) -> discord.abc.GuildChannel | discord.Thread: ...

    @overload
    async def get_or_fetch_channel(
        self,
        channel_id: int,
        *,
        guild: None = None,
    ) -> discord.abc.GuildChannel | discord.abc.PrivateChannel | discord.Thread: ...

    async def get_or_fetch_channel(
        self,
        channel_id: int,
        *,
        guild: discord.Guild | None = None,
    ) -> discord.abc.GuildChannel | discord.abc.PrivateChannel | discord.Thread:
        """Return a cached channel or fetch it from Discord."""

        if guild is not None:
            channel = guild.get_channel_or_thread(channel_id)
            if channel is not None:
                return channel

            return await guild.fetch_channel(channel_id)

        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel

        return await self.fetch_channel(channel_id)

    async def get_or_fetch_guild(self, guild_id: int) -> discord.Guild:
        """Return a cached guild or fetch it from Discord."""

        guild = self.get_guild(guild_id)
        if guild is not None:
            return guild

        return await self.fetch_guild(guild_id)

    async def get_or_fetch_message(
        self,
        channel: discord.abc.Messageable,
        message_id: int,
    ) -> discord.Message:
        """Return a cached message or fetch it from its channel."""

        message = discord.utils.get(self.cached_messages, id=message_id)
        if message is not None:
            return message

        return await channel.fetch_message(message_id)

    @property
    def config(self) -> Any:
        return __import__('settings')

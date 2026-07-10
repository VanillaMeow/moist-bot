# pyright: standard

from __future__ import annotations

import asyncio
import inspect
import itertools
import sys
import unicodedata
from importlib.metadata import distribution, packages_distributions
from typing import TYPE_CHECKING, Any, cast

import discord
import psutil
from discord.ext import commands, menus
from jishaku.modules import package_version

from moist_bot.constants import PROJECT_ROOT_PATH
from moist_bot.utils.checks import has_guild_permissions_or_dm
from moist_bot.utils.paginator import RoboPages
from moist_bot.utils.process import run_git

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.cogs.stats import Stats
    from moist_bot.utils.context import Context


"""
Most of this is taken/edited from:
https://github.com/Rapptz/RoboDanny
"""


class GroupHelpPageSource(menus.ListPageSource):
    entries: list[commands.Command[Any, ..., Any]]

    def __init__(
        self,
        group: commands.Group[Any, ..., Any] | commands.Cog,
        entries: list[commands.Command[Any, ..., Any]],
        *,
        prefix: str,
    ):
        super().__init__(entries=entries, per_page=6)
        self.group: commands.Group[Any, ..., Any] | commands.Cog = group
        self.prefix: str = prefix
        self.title: str = f'{self.group.qualified_name} Commands'
        self.description: str = self.group.description

    async def format_page(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, menu: RoboPages, cmds: list[commands.Command[Any, ..., Any]]
    ) -> discord.Embed:
        embed = discord.Embed(
            title=self.title,
            description=self.description,
            colour=discord.Colour(0xA8B9CD),
        )

        for command in cmds:
            signature = f'{command.qualified_name} {command.signature}'
            embed.add_field(
                name=signature,
                value=command.short_doc or '',
                inline=False,
            )

        maximum = self.get_max_pages()
        if maximum > 1:
            embed.set_author(
                name=f'Page {menu.current_page + 1}/{maximum} ({len(self.entries)} commands)'
            )

        embed.set_footer(
            text=f'Use "{self.prefix}help command" for more info on a command.'
        )
        return embed


class HelpSelectMenu(discord.ui.Select['HelpMenu']):
    def __init__(
        self,
        entries: dict[commands.Cog, list[commands.Command[Any, ..., Any]]],
        bot: MoistBot,
    ):
        super().__init__(
            placeholder='Select a category...',
            min_values=1,
            max_values=1,
            row=0,
        )
        self.commands: dict[commands.Cog, list[commands.Command[Any, ..., Any]]] = (
            entries
        )
        self.bot: MoistBot = bot
        self.__fill_options()

    def __fill_options(self) -> None:
        self.add_option(
            label='Index',
            emoji='\N{WAVING HAND SIGN}',
            value='__index',
            description='The help page showing how to use the bot.',
        )
        for cog, cmds in self.commands.items():
            if not cmds:
                continue
            description = cog.description.split('\n', 1)[0] or None
            emoji = getattr(cog, 'display_emoji', None)
            self.add_option(
                label=cog.qualified_name,
                value=cog.qualified_name,
                description=description,
                emoji=emoji,
            )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            m = 'View is None'
            raise RuntimeError(m)

        value = self.values[0]
        if value == '__index':
            await self.view.rebind(FrontPageSource(), interaction)
        else:
            cog = self.bot.get_cog(value)
            if cog is None:
                await interaction.response.send_message(
                    'Somehow this category does not exist?', ephemeral=True
                )
                return

            cmds = self.commands[cog]
            if not cmds:
                await interaction.response.send_message(
                    'This category has no commands for you', ephemeral=True
                )
                return

            source = GroupHelpPageSource(cog, cmds, prefix=self.view.ctx.clean_prefix)
            await self.view.rebind(source, interaction)


class FrontPageSource(menus.PageSource):
    index: int

    def is_paginating(self) -> bool:
        # This forces the buttons to appear even in the front page
        return True

    def get_max_pages(self) -> int:  # pyright: ignore[reportIncompatibleMethodOverride]
        # There's only one actual page in the front page
        # However we need at least 2 to show all the buttons
        return 2

    async def get_page(self, page_number: int) -> Any:
        # The front page is a dummy
        self.index = page_number
        return self

    async def format_page(self, menu: HelpMenu, page: Any) -> discord.Embed:  # noqa: ARG002
        embed = discord.Embed(title='Bot Help', colour=discord.Colour(0xA8B9CD))
        embed.description = inspect.cleandoc(
            f"""
            Hello! Welcome to the help page.

            Use "{menu.ctx.clean_prefix}help command" for more info on a command.
            Use "{menu.ctx.clean_prefix}help category" for more info on a category.
            Use the dropdown menu below to select a category.
        """
        )

        if self.index == 0:
            entries = (
                ('<argument>', 'This means the argument is __**required**__.'),
                ('[argument]', 'This means the argument is __**optional**__.'),
                ('[A|B]', 'This means that it can be __**either A or B**__.'),
                (
                    '[argument...]',
                    (
                        'This means you can have multiple arguments.\n'
                        'Now that you know the basics, it should be noted that...\n'
                        '__**You do not type in the brackets!**__'
                    ),
                ),
            )

            embed.add_field(
                name='How do I use this bot?',
                value='Reading the bot signature is pretty simple.',
            )

            for name, value in entries:
                embed.add_field(name=name, value=value, inline=False)

        return embed


class HelpMenu(RoboPages):
    def __init__(self, source: menus.PageSource, ctx: Context):
        super().__init__(source, ctx=ctx, compact=True)

    def add_categories(
        self, cmds: dict[commands.Cog, list[commands.Command[Any, ..., Any]]]
    ) -> None:
        self.clear_items()
        self.add_item(HelpSelectMenu(cmds, self.ctx.bot))
        self.fill_items()

    async def rebind(
        self, source: menus.PageSource, interaction: discord.Interaction
    ) -> None:
        self.source = source
        self.current_page = 0

        await self.source._prepare_once()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        page = await self.source.get_page(0)
        kwargs = await self._get_kwargs_from_page(page)
        self._update_labels(0)
        await interaction.response.edit_message(**kwargs, view=self)


class PaginatedHelpCommand(commands.HelpCommand):
    context: Context  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(self):
        super().__init__(
            command_attrs={
                'cooldown': commands.CooldownMapping.from_cooldown(
                    1, 3.0, commands.BucketType.member
                ),
                'help': 'Shows help about the bot, a command, or a category',
            }
        )

    async def on_help_command_error(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, ctx: Context, error: commands.CommandError, /
    ) -> None:
        if isinstance(error, commands.CommandInvokeError):
            # Ignore missing permission errors
            if (
                isinstance(error.original, discord.HTTPException)
                and error.original.code == 50013
            ):
                return

            await ctx.reply(str(error.original))

    async def send_error_message(self, error: str, /) -> None:
        await self.context.reply(error)

    def get_command_signature(self, command: commands.Command[Any, ..., Any], /) -> str:
        parent = command.full_parent_name
        if len(command.aliases) > 0:
            aliases = '|'.join(command.aliases)
            fmt = f'[{command.name}|{aliases}]'
            if parent:
                fmt = f'{parent} {fmt}'
            alias = fmt
        else:
            alias = command.name if not parent else f'{parent} {command.name}'
        return f'{alias} {command.signature}'

    async def send_bot_help(self, _mapping: Any, /) -> None:
        bot = self.context.bot

        def key(command: commands.Command[Any, ..., Any]) -> str:
            cog = command.cog
            return cog.qualified_name if cog else '\U0010ffff'

        entries: list[commands.Command[Any, ..., Any]] = await self.filter_commands(
            bot.commands, sort=True, key=key
        )

        all_commands: dict[commands.Cog, list[commands.Command[Any, ..., Any]]] = {}
        for name, children in itertools.groupby(entries, key=key):
            if name == '\U0010ffff':
                continue

            cog = bot.get_cog(name)
            assert cog is not None  # noqa: S101
            all_commands[cog] = sorted(children, key=lambda c: c.qualified_name)

        menu = HelpMenu(FrontPageSource(), ctx=self.context)
        menu.add_categories(all_commands)
        await menu.start()

    async def send_cog_help(self, cog: commands.Cog, /) -> None:
        entries = await self.filter_commands(cog.get_commands(), sort=True)
        menu = HelpMenu(
            GroupHelpPageSource(cog, entries, prefix=self.context.clean_prefix),
            ctx=self.context,
        )
        await menu.start()

    def common_command_formatting(
        self,
        embed_like: discord.Embed | GroupHelpPageSource,
        command: commands.Command[Any, ..., Any],
    ) -> None:
        embed_like.title = self.get_command_signature(command)
        if command.description:
            embed_like.description = f'{command.description}\n\n{command.help}'
        else:
            embed_like.description = command.help or ''

    async def send_command_help(
        self, command: commands.Command[Any, ..., Any], /
    ) -> None:
        # No pagination necessary for a single command.
        embed = discord.Embed(colour=discord.Colour(0xA8B9CD))
        self.common_command_formatting(embed, command)
        await self.context.reply(embed=embed)

    async def send_group_help(self, group: commands.Group[Any, ..., Any], /) -> None:
        subcommands = group.commands
        if len(subcommands) == 0:
            return await self.send_command_help(group)

        entries = await self.filter_commands(subcommands, sort=True)
        if len(entries) == 0:
            return await self.send_command_help(group)

        source = GroupHelpPageSource(group, entries, prefix=self.context.clean_prefix)
        self.common_command_formatting(source, group)
        menu = HelpMenu(source, ctx=self.context)
        await menu.start()


class Meta(commands.Cog):
    """Commands for utilities related to Discord or the Bot itself."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

        self.old_help_command: commands.HelpCommand | None = bot.help_command
        bot.help_command = PaginatedHelpCommand()
        bot.help_command.cog = self
        self.process = psutil.Process()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{WHITE QUESTION MARK ORNAMENT}')

    async def cog_unload(self) -> None:
        self.bot.help_command = self.old_help_command

    async def cog_command_error(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, ctx: Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.reply(str(error))

    @commands.command()
    @commands.cooldown(rate=1, per=8, type=commands.BucketType.member)
    @has_guild_permissions_or_dm(manage_messages=True)
    async def charinfo(self, ctx: Context, *, characters: str):
        """Shows you information about a number of characters.

        Only up to 25 characters at a time.
        """

        def to_string(c: str) -> str:
            digit = f'{ord(c):x}'
            name = unicodedata.name(c, 'Name not found.')
            return f'`\\U{digit:>08}`: {name} - {c} \N{EM DASH} <http://www.fileformat.info/info/unicode/char/{digit}>'

        msg = '\n'.join(map(to_string, characters))
        if len(msg) > 2000:
            return await ctx.reply('Output too long to display.')
        await ctx.reply(msg)

    @commands.command(name='health', aliases=['about'])
    async def _bot_stats(self, ctx: Context):
        """Various bot stat monitoring tools."""

        HEALTHY = discord.Color(value=0x43B581)  # noqa: N806
        UNHEALTHY = discord.Color(value=0xF04947)  # noqa: N806
        # WARNING = discord.Color(value=0xF09E47)

        # Process stats
        process = self.process
        with process.oneshot():
            cpu_count = psutil.cpu_count() or 1
            cpu_usage = process.cpu_percent() / cpu_count
            thread_count = process.num_threads()
            memory = process.memory_full_info()
            system_memory = psutil.virtual_memory()

            physical_memory = memory.rss / 1024**2
            unique_memory = memory.uss / 1024**2
            free_memory = system_memory.available / 1024**2

        # Message cache stats
        if self.bot._connection.max_messages:  # noqa: SLF001
            message_cache = (
                f'{len(self.bot.cached_messages)}/{self.bot._connection.max_messages}'  # noqa: SLF001
            )
        else:
            message_cache = 'Disabled'

        # Tasks stats
        all_tasks = asyncio.all_tasks(loop=self.bot.loop)
        event_tasks = [
            t for t in all_tasks if 'Client._run_event' in repr(t) and not t.done()
        ]

        future_tasks = [t for t in event_tasks if 'Future pending' in repr(t)]

        # # Distribution stats
        # Try to locate what vends the `discord` package
        distributions: list[str] = [
            dist
            for dist in packages_distributions()['discord']  # type: ignore[]
            if any(
                file.parts == ('discord', '__init__.py')  # type: ignore[]
                for file in distribution(dist).files  # type: ignore[]
            )
        ]

        if distributions:
            dist_version = f'{distributions[0]}: v{package_version(distributions[0])}'
        else:
            dist_version = f'unknown: v{discord.__version__}'

        commit_status, commit_stdout, _ = await run_git(
            'rev-parse', '--short', 'HEAD', cwd=PROJECT_ROOT_PATH
        )
        current_commit = commit_stdout.strip() if commit_status == 0 else 'unknown'

        python_version, _, _ = sys.version.partition('(')

        stats_cog = self.bot.get_cog('Stats')
        commands_run = 0
        socket_events = 0
        total_socket_events = 0
        if stats_cog is not None:
            stats = cast('Stats', stats_cog)
            commands_run = sum(stats.command_stats.values())
            socket_events = sum(stats.socket_stats.values())
            total_socket_events = stats.total_socket_events

        embed = (
            discord.Embed(
                title='Bot Stats Report',
                color=HEALTHY,
                timestamp=discord.utils.utcnow(),
            )
            .add_field(
                name='Process',
                value=f'{cpu_usage:.2f}% CPU\n'
                f'CPU Threads: {cpu_count}\n'
                f'Process Threads: {thread_count}\n',
                inline=True,
            )
            .add_field(
                name='Memory',
                value=f'Physical: {physical_memory:.2f} MiB\n'
                f'Unique: {unique_memory:.2f} MiB\n'
                f'Free: {free_memory:.2f} MiB',
                inline=True,
            )
            .add_field(
                name='Cache',
                value=f'Guilds: {len(self.bot.guilds)}\n'
                f'Users: {len(self.bot.users)}\n'
                f'Messages: {message_cache}',
                inline=True,
            )
            .add_field(
                name='Events Waiting',
                value=f'Total: {len(event_tasks)}\nFuture task: {len(future_tasks)}',
                inline=True,
            )
            .add_field(
                name='Session Counters',
                value=f'Commands run: {commands_run!s}\n'
                f'Socket events: {socket_events!s}\n'
                f'Total socket events: {total_socket_events!s}',
                inline=True,
            )
            .add_field(
                name='Distribution',
                value=f'Commit: `{current_commit}`\n'
                f'{dist_version}\n'
                f'Jishaku: v{package_version("jishaku")}\n'
                f'Python: v{python_version}\n'
                f'Platform: {sys.platform}',
                inline=False,
            )
            .set_footer(text='Made with ❤️ by Leah 🌸')
        )

        description: list[str] = []

        started_at = discord.utils.format_dt(self.bot.started_at, 'R')
        description.append(f'Started: {started_at}')

        global_rate_limit = not self.bot.http._global_over.is_set()  # noqa: SLF001
        description.append(f'Global Rate Limit: {global_rate_limit}')

        if global_rate_limit:
            embed.color = UNHEALTHY

        embed.description = '\n'.join(description)
        await ctx.reply(embed=embed)

    @commands.command(hidden=True)
    @has_guild_permissions_or_dm(manage_messages=True)
    @commands.cooldown(1, 30.0, type=commands.BucketType.member)
    async def cud(self, ctx: Context):
        """pls no spam"""

        for i in range(3):
            await ctx.send(str(3 - i))
            await asyncio.sleep(1)

        await ctx.send('go')


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(Meta(bot))

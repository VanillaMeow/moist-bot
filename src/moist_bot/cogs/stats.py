# ruff: noqa: PLR0904
# pyright: standard

from __future__ import annotations

import asyncio
import datetime
import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, menus, tasks
from sqlmodel import col

from moist_bot.models import (
    CommandStatsScope,
    CommandUsage,
    CommandUsageCommandCount,
    CommandUsageGuildCount,
    CommandUsageStats,
    CommandUsageUserCount,
    SocketEventStats,
)
from moist_bot.utils import formats
from moist_bot.utils.converters import normalize_datetime, shorten
from moist_bot.utils.formats import plural
from moist_bot.utils.paginator import RoboPages

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.sql.elements import ColumnElement

    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context, GuildContext, Interaction


log = logging.getLogger('discord.' + __name__)

HISTORY_COMMAND_WIDTH = 64
HISTORY_PAGE_SIZE = 8


async def can_view_history(ctx: GuildContext) -> bool:
    if await ctx.bot.is_owner(ctx.author):
        return True

    permissions = ctx.channel.permissions_for(ctx.author)
    if ctx.author.guild_permissions.manage_guild or permissions.manage_messages:
        return True

    msg = 'You need Manage Server or Manage Messages to view command history.'
    raise commands.CheckFailure(msg)


@dataclass(frozen=True, slots=True)
class CommandHistoryPage:
    page_number: int
    rows: list[CommandUsage]


def format_history_table(
    rows: Iterable[CommandUsage],
    *,
    start_index: int,
    include_author: bool,
    include_guild: bool,
) -> str:
    table = formats.TabularData()

    # Set the columns
    columns = ['#', 'Used', 'State', 'Mode']
    if include_author:
        columns.append('Author')
    if include_guild:
        columns.append('Guild')
    columns.append('Command')
    table.set_columns(columns)

    rendered_rows = []
    for index, command_usage in enumerate(rows, start=start_index):
        used_at = normalize_datetime(command_usage.used_at)
        used = used_at.strftime('%Y-%m-%d %H:%M')
        guild_id = command_usage.guild_id

        row = [
            str(index),
            used,
            'fail' if command_usage.failed else 'ok',
            'slash' if command_usage.app_command else 'text',
        ]

        if include_author:
            row.append(str(command_usage.author_id))
        if include_guild:
            row.append('DM' if guild_id is None else str(guild_id))

        row.append(shorten(command_usage.command, HISTORY_COMMAND_WIDTH))
        rendered_rows.append(row)

    table.add_rows(rendered_rows)
    return table.render()


class CommandHistoryPageSource(menus.PageSource):
    def __init__(
        self,
        bot: MoistBot,
        *,
        title: str,
        criteria: Iterable[ColumnElement[bool]] = (),
        summary: str | None = None,
        include_author: bool = False,
        include_guild: bool = False,
        per_page: int = HISTORY_PAGE_SIZE,
    ) -> None:
        self.bot: MoistBot = bot
        self.title: str = title
        self.criteria: tuple[ColumnElement[bool], ...] = tuple(criteria)
        self.summary: str | None = summary
        self.include_author: bool = include_author
        self.include_guild: bool = include_guild
        self.per_page: int = per_page
        self.total_entries: int = 0

    async def prepare(self) -> None:
        async with self.bot.db_session_maker() as session:
            total_entries = await CommandUsage.history_count(
                session,
                criteria=self.criteria,
            )

        self.total_entries = total_entries

    def is_paginating(self) -> bool:
        return self.total_entries > self.per_page

    def get_max_pages(self) -> int:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.total_entries == 0:
            return 1
        return (self.total_entries + self.per_page - 1) // self.per_page

    async def get_page(self, page_number: int) -> CommandHistoryPage:
        offset = page_number * self.per_page
        limit = self.per_page

        async with self.bot.db_session_maker() as session:
            rows = await CommandUsage.history(
                session,
                limit=limit,
                offset=offset,
                criteria=self.criteria,
            )

        if not rows and page_number > 0:
            raise IndexError
        return CommandHistoryPage(page_number=page_number, rows=rows)

    async def format_page(
        self,
        menu: RoboPages,
        page: CommandHistoryPage,
    ) -> str:
        lines = [f'**{self.title}**']
        if self.summary is not None:
            lines.append(self.summary)

        if not page.rows:
            lines.append('No command history found.')
            return '\n'.join(lines)

        table = format_history_table(
            page.rows,
            start_index=(page.page_number * self.per_page) + 1,
            include_author=self.include_author,
            include_guild=self.include_guild,
        )
        lines.append(f'```\n{table}\n```')

        maximum = self.get_max_pages()
        if maximum > 1:
            lines.append(
                f'Page {menu.current_page + 1}/{maximum} '
                f'({plural(self.total_entries):entry})'
            )

        return '\n'.join(lines)


async def send_history_paginator(
    ctx: Context,
    bot: MoistBot,
    *,
    title: str,
    criteria: Iterable[ColumnElement[bool]] = (),
    summary: str | None = None,
    include_author: bool = False,
    include_guild: bool = False,
) -> None:
    source = CommandHistoryPageSource(
        bot,
        title=title,
        criteria=criteria,
        summary=summary,
        include_author=include_author,
        include_guild=include_guild,
    )
    await source._prepare_once()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    pages = RoboPages(source, ctx=ctx, check_embeds=False)
    await pages.start()


class Stats(commands.Cog):
    """Bot usage statistics."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

        self._batch_lock = asyncio.Lock()
        self._data_batch: list[CommandUsage] = []
        self._socket_stats_batch: Counter[str] = Counter()

        self.command_stats: Counter[str] = Counter()
        self.command_types_used: Counter[bool] = Counter()
        self.socket_stats: Counter[str] = Counter()
        self._total_socket_events: int = 0

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{BAR CHART}')

    @property
    def total_socket_events(self) -> int:
        return self._total_socket_events

    async def cog_load(self) -> None:
        async with self.bot.db_session_maker() as session:
            total_socket_events = await SocketEventStats.total_events_count(session)

        async with self._batch_lock:
            self._total_socket_events = total_socket_events + sum(
                self._socket_stats_batch.values()
            )

        self.bulk_insert_loop.start()

    async def cog_unload(self) -> None:
        self.bulk_insert_loop.cancel()
        async with self._batch_lock:
            await self.bulk_insert()

    @tasks.loop(seconds=10.0)
    async def bulk_insert_loop(self) -> None:
        async with self._batch_lock:
            await self.bulk_insert()

    async def bulk_insert(self) -> None:
        has_data_batch = bool(self._data_batch)
        has_socket_stats_batch = bool(self._socket_stats_batch)

        if not has_data_batch and not has_socket_stats_batch:
            return

        async with self.bot.db_session_maker() as session:
            if has_data_batch:
                session.add_all(self._data_batch)
                await session.flush()
                await CommandUsageStats.upsert_usage_batch(
                    session,
                    self._data_batch,
                )

            if has_socket_stats_batch:
                await SocketEventStats.upsert_event_batch(
                    session,
                    self._socket_stats_batch,
                )

            await session.commit()

        self._data_batch.clear()
        self._socket_stats_batch.clear()

    async def register_command(
        self,
        ctx: Context,
        *,
        failed: bool | None = None,
    ) -> None:
        if ctx.command is None:
            return

        command = ctx.command.qualified_name
        is_app_command = ctx.interaction is not None
        failed = ctx.command_failed if failed is None else failed
        self.command_stats[command] += 1
        self.command_types_used[is_app_command] += 1

        guild_id = None if ctx.guild is None else ctx.guild.id

        async with self._batch_lock:
            self._data_batch.append(
                CommandUsage(
                    guild_id=guild_id,
                    channel_id=ctx.channel.id,
                    author_id=ctx.author.id,
                    used_at=ctx.message.created_at,
                    prefix=ctx.prefix,
                    command=command,
                    failed=failed,
                    app_command=is_app_command,
                )
            )

    async def register_interaction(self, interaction: Interaction) -> None:
        command = interaction.command
        if command is None:
            return

        command_name = command.qualified_name
        self.command_stats[command_name] += 1
        self.command_types_used[True] += 1

        async with self._batch_lock:
            self._data_batch.append(
                CommandUsage(
                    guild_id=interaction.guild_id,
                    channel_id=interaction.channel_id or 0,
                    author_id=interaction.user.id,
                    used_at=interaction.created_at,
                    prefix='/',
                    command=command_name,
                    failed=bool(getattr(interaction, 'command_failed', False)),
                    app_command=True,
                )
            )

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: Context) -> None:
        await self.register_command(ctx, failed=False)

    @commands.Cog.listener()
    async def on_command_error(
        self, ctx: Context, _error: commands.CommandError
    ) -> None:
        await self.register_command(ctx, failed=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: Interaction) -> None:
        command = interaction.command
        if (
            command is not None
            and interaction.type is discord.InteractionType.application_command
            and not command.__class__.__name__.startswith('Hybrid')
        ):
            await self.register_interaction(interaction)

    @commands.Cog.listener()
    async def on_socket_event_type(self, event_type: str) -> None:
        self.socket_stats[event_type] += 1
        async with self._batch_lock:
            self._socket_stats_batch[event_type] += 1
            self._total_socket_events += 1

    @staticmethod
    def format_count_rows(
        rows: Iterable[CommandUsageCommandCount],
        *,
        empty: str,
    ) -> str:
        lines = []
        for index, row in enumerate(rows, start=1):
            lines.append(f'{index}. `{row.label}` ({plural(row.uses):use})')
        return '\n'.join(lines) or empty

    @staticmethod
    def format_user_rows(
        rows: Iterable[CommandUsageUserCount],
        *,
        empty: str,
    ) -> str:
        lines = []
        for index, row in enumerate(rows, start=1):
            lines.append(f'{index}. <@{row.author_id}> ({plural(row.uses):use})')
        return '\n'.join(lines) or empty

    def format_guild_rows(
        self,
        rows: Iterable[CommandUsageGuildCount],
        *,
        empty: str,
    ) -> str:
        lines = []
        for index, row in enumerate(rows, start=1):
            if row.guild_id is None:
                guild = 'Private Message'
            else:
                guild = str(
                    self.bot.get_guild(row.guild_id) or f'<Unknown {row.guild_id}>'
                )
            lines.append(f'{index}. {guild} ({plural(row.uses):use})')
        return '\n'.join(lines) or empty

    async def show_guild_stats(self, ctx: GuildContext) -> None:
        async with self.bot.db_session_maker() as session:
            summary = await CommandUsageStats.count_and_first(
                session,
                CommandStatsScope.GUILD,
                guild_id=ctx.guild.id,
            )
            top_commands = await CommandUsageStats.top_commands(
                session,
                CommandStatsScope.GUILD_COMMAND,
                guild_id=ctx.guild.id,
            )
            top_commands_today = await CommandUsage.top_commands(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.used_at)
                > discord.utils.utcnow() - datetime.timedelta(days=1),
            )
            top_users = await CommandUsageStats.top_users(
                session,
                CommandStatsScope.GUILD_USER,
                guild_id=ctx.guild.id,
            )
            top_users_today = await CommandUsage.top_users(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.used_at)
                > discord.utils.utcnow() - datetime.timedelta(days=1),
            )

        embed = (
            discord.Embed(
                title='Server Command Stats',
                description=f'{plural(summary.total_uses):command} used.',
                colour=ctx.me.colour,
                timestamp=summary.first_used or discord.utils.utcnow(),
            )
            .set_footer(text='Tracking command usage since')
            .add_field(
                name='Top Commands',
                value=self.format_count_rows(top_commands, empty='No commands.'),
                inline=True,
            )
            .add_field(
                name='Top Commands Today',
                value=self.format_count_rows(top_commands_today, empty='No commands.'),
                inline=True,
            )
            .add_field(
                name='Top Command Users',
                value=self.format_user_rows(top_users, empty='No command users.'),
                inline=True,
            )
            .add_field(
                name='Top Command Users Today',
                value=self.format_user_rows(top_users_today, empty='No command users.'),
                inline=True,
            )
        )
        await ctx.reply(embed=embed)

    async def show_member_stats(
        self, ctx: GuildContext, member: discord.Member
    ) -> None:
        async with self.bot.db_session_maker() as session:
            summary = await CommandUsageStats.count_and_first(
                session,
                CommandStatsScope.GUILD_USER,
                guild_id=ctx.guild.id,
                author_id=member.id,
            )
            top_commands = await CommandUsageStats.top_commands(
                session,
                CommandStatsScope.GUILD_USER_COMMAND,
                guild_id=ctx.guild.id,
                author_id=member.id,
            )
            top_commands_today = await CommandUsage.top_commands(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.author_id) == member.id,
                col(CommandUsage.used_at)
                > discord.utils.utcnow() - datetime.timedelta(days=1),
            )

        embed = (
            discord.Embed(
                title='Command Stats',
                description=f'{plural(summary.total_uses):command} used.',
                colour=member.colour,
                timestamp=summary.first_used or discord.utils.utcnow(),
            )
            .set_author(name=str(member), icon_url=member.display_avatar.url)
            .set_footer(text='First command used')
            .add_field(
                name='Most Used Commands',
                value=self.format_count_rows(top_commands, empty='No commands.'),
                inline=False,
            )
            .add_field(
                name='Most Used Commands Today',
                value=self.format_count_rows(top_commands_today, empty='No commands.'),
                inline=False,
            )
        )
        await ctx.reply(embed=embed)

    @commands.group(
        aliases=['statistics'],
        invoke_without_command=True,
        usage='[member]',
    )
    @commands.guild_only()
    @commands.cooldown(1, 30.0, type=commands.BucketType.member)
    async def stats(
        self, ctx: GuildContext, *, member: discord.Member | None = None
    ) -> None:
        """Tells you command usage stats for the server or a member."""
        async with ctx.typing():
            if member is None:
                await self.show_guild_stats(ctx)
            else:
                await self.show_member_stats(ctx, member)

    @stats.command(name='global')
    @commands.is_owner()
    async def stats_global(self, ctx: Context) -> None:
        """Global all-time command statistics."""
        async with self.bot.db_session_maker() as session:
            summary = await CommandUsageStats.count_and_first(
                session,
                CommandStatsScope.GLOBAL,
            )
            top_commands = await CommandUsageStats.top_commands(
                session,
                CommandStatsScope.GLOBAL_COMMAND,
            )
            top_guilds = await CommandUsageStats.top_guilds(session)
            top_users = await CommandUsageStats.top_users(
                session,
                CommandStatsScope.GLOBAL_USER,
            )

        embed = (
            discord.Embed(
                title='Command Stats',
                colour=discord.Colour.blurple(),
                description=f'{plural(summary.total_uses):command} used.',
            )
            .add_field(
                name='Top Commands',
                value=self.format_count_rows(top_commands, empty='No commands.'),
                inline=False,
            )
            .add_field(
                name='Top Guilds',
                value=self.format_guild_rows(top_guilds, empty='No guilds.'),
                inline=False,
            )
            .add_field(
                name='Top Users',
                value=self.format_user_rows(top_users, empty='No users.'),
                inline=False,
            )
        )
        await ctx.reply(embed=embed)

    @stats.command(name='today')
    @commands.is_owner()
    async def stats_today(self, ctx: Context) -> None:
        """Global command statistics for the last 24 hours."""
        since = discord.utils.utcnow() - datetime.timedelta(days=1)

        async with self.bot.db_session_maker() as session:
            states = await CommandUsage.failed_counts(
                session,
                col(CommandUsage.used_at) > since,
            )
            top_commands = await CommandUsage.top_commands(
                session,
                col(CommandUsage.used_at) > since,
            )
            top_guilds = await CommandUsage.top_guilds(
                session,
                col(CommandUsage.used_at) > since,
            )
            top_users = await CommandUsage.top_users(
                session,
                col(CommandUsage.used_at) > since,
            )

        success = 0
        failed = 0
        for row in states:
            if row.failed:
                failed += row.uses
            else:
                success += row.uses

        embed = (
            discord.Embed(
                title='Last 24 Hour Command Stats',
                description=(
                    f'{plural(success + failed):command} used today. '
                    f'({success} succeeded, {failed} failed)'
                ),
                colour=discord.Colour.blurple(),
            )
            .add_field(
                name='Top Commands',
                value=self.format_count_rows(top_commands, empty='No commands.'),
                inline=False,
            )
            .add_field(
                name='Top Guilds',
                value=self.format_guild_rows(top_guilds, empty='No guilds.'),
                inline=False,
            )
            .add_field(
                name='Top Users',
                value=self.format_user_rows(top_users, empty='No users.'),
                inline=False,
            )
        )
        await ctx.reply(embed=embed)

    @stats.command(name='session')
    @commands.is_owner()
    async def stats_session(self, ctx: Context, limit: int = 12) -> None:
        """Shows current-process command statistics."""
        if limit == 0:
            raise commands.BadArgument('Limit must not be 0.')

        total = sum(self.command_stats.values())
        slash_commands = self.command_types_used[True]
        delta = discord.utils.utcnow() - self.bot.started_at
        minutes = max(delta.total_seconds() / 60, 1 / 60)
        cpm = total / minutes

        if limit > 0:
            common = self.command_stats.most_common(limit)
            title = f'Top {limit} Session Commands'
        else:
            common = self.command_stats.most_common()[limit:]
            title = f'Bottom {abs(limit)} Session Commands'

        rows = [(command, f'{uses} uses') for command, uses in common]
        source = formats.TabularData()
        source.set_columns(['Command', 'Uses'])
        source.add_rows(rows)
        table = source.render()

        await ctx.reply(
            f'**{title}**\n'
            f'{total} total commands used '
            f'({slash_commands} slash command uses, {cpm:.2f}/minute)\n'
            f'```\n{table}\n```'
        )

    @stats.group(name='history', invoke_without_command=True)
    @commands.guild_only()
    async def stats_history(
        self,
        ctx: GuildContext,
        *,
        member: discord.Member = commands.Author,
    ) -> None:
        """Shows command history for a member in this server."""
        if member.id != ctx.author.id:
            await can_view_history(ctx)

        criteria = (
            col(CommandUsage.guild_id) == ctx.guild.id,
            col(CommandUsage.author_id) == member.id,
        )
        await send_history_paginator(
            ctx,
            self.bot,
            title=f'Command History for {member}',
            criteria=criteria,
        )

    @stats_history.command(name='command')
    @commands.check(can_view_history)
    async def stats_history_command(
        self,
        ctx: GuildContext,
        command: str,
        days: int = 7,
    ) -> None:
        """Shows recent command history for a command in this server."""
        if days < 1:
            raise commands.BadArgument('Days must be at least 1.')

        since = discord.utils.utcnow() - datetime.timedelta(days=days)

        criteria = (
            col(CommandUsage.guild_id) == ctx.guild.id,
            col(CommandUsage.command) == command,
            col(CommandUsage.used_at) > since,
        )
        async with self.bot.db_session_maker() as session:
            states = await CommandUsage.failed_counts(session, *criteria)

        success = 0
        failed = 0
        for row in states:
            if row.failed:
                failed += row.uses
            else:
                success += row.uses

        await send_history_paginator(
            ctx,
            self.bot,
            title=f'Recent `{command}` Commands',
            criteria=criteria,
            summary=(
                f'`{command}` in the last {plural(days):day}: '
                f'{success} succeeded, {failed} failed.'
            ),
            include_author=True,
        )

    @stats_history.command(name='global')
    @commands.is_owner()
    async def stats_history_global(self, ctx: Context) -> None:
        """Shows recent command history across every guild."""
        await send_history_paginator(
            ctx,
            self.bot,
            title='Recent Global Commands',
            include_author=True,
            include_guild=True,
        )


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(Stats(bot))

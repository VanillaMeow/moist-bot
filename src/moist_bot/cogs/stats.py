# ruff: noqa: PLR0904
# pyright: standard

from __future__ import annotations

import asyncio
import datetime
import logging
import platform
from collections import Counter
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, TypedDict, cast

import discord
import psutil
from discord.ext import commands, tasks
from sqlalchemy import func
from sqlmodel import col, select

from moist_bot.db.models import CommandUsage
from moist_bot.utils import formats, time

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context, GuildContext


log = logging.getLogger('discord.' + __name__)

MAX_HISTORY_LIMIT = 50
MAX_HISTORY_DAYS = 90


class CommandBatchEntry(TypedDict):
    guild_id: int | None
    channel_id: int
    author_id: int
    used_at: datetime.datetime
    prefix: str
    command: str
    failed: bool
    app_command: bool


class Stats(commands.Cog):
    """Bot usage statistics."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot
        self.process = psutil.Process()
        self._batch_lock = asyncio.Lock()
        self._data_batch: list[CommandBatchEntry] = []
        self.command_stats: Counter[str] = Counter()
        self.command_types_used: Counter[bool] = Counter()
        self.socket_stats: Counter[str] = Counter()
        self.bulk_insert_loop.start()

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{BAR CHART}')

    async def cog_unload(self) -> None:
        self.bulk_insert_loop.cancel()
        async with self._batch_lock:
            await self.bulk_insert()

    @tasks.loop(seconds=10.0)
    async def bulk_insert_loop(self) -> None:
        async with self._batch_lock:
            await self.bulk_insert()

    async def bulk_insert(self) -> None:
        if not self._data_batch:
            return

        entries = [
            CommandUsage(
                guild_id=entry['guild_id'],
                channel_id=entry['channel_id'],
                author_id=entry['author_id'],
                used_at=entry['used_at'],
                prefix=entry['prefix'],
                command=entry['command'],
                failed=entry['failed'],
                app_command=entry['app_command'],
            )
            for entry in self._data_batch
        ]

        async with self.bot.db_session_maker() as session:
            session.add_all(entries)
            await session.commit()

        total = len(entries)
        if total > 1:
            log.info('Registered %s commands to the database.', total)
        self._data_batch.clear()

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

        if ctx.guild is None:
            guild_id = None
            destination = 'Private Message'
        else:
            guild_id = ctx.guild.id
            destination = f'#{ctx.channel} ({ctx.guild})'

        log.info(
            '%s: %s in %s: %s',
            ctx.message.created_at,
            ctx.author,
            destination,
            ctx.message.content,
        )

        async with self._batch_lock:
            self._data_batch.append(
                {
                    'guild_id': guild_id,
                    'channel_id': ctx.channel.id,
                    'author_id': ctx.author.id,
                    'used_at': ctx.message.created_at,
                    'prefix': ctx.prefix,
                    'command': command,
                    'failed': failed,
                    'app_command': is_app_command,
                }
            )

    async def register_interaction(self, interaction: discord.Interaction) -> None:
        command = interaction.command
        if command is None:
            return

        command_name = command.qualified_name
        self.command_stats[command_name] += 1
        self.command_types_used[True] += 1

        async with self._batch_lock:
            self._data_batch.append(
                {
                    'guild_id': interaction.guild_id,
                    'channel_id': interaction.channel_id or 0,
                    'author_id': interaction.user.id,
                    'used_at': interaction.created_at,
                    'prefix': '/',
                    'command': command_name,
                    'failed': bool(getattr(interaction, 'command_failed', False)),
                    'app_command': True,
                }
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
    async def on_interaction(self, interaction: discord.Interaction) -> None:
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

    @staticmethod
    def clamp_limit(limit: int) -> int:
        return max(1, min(limit, MAX_HISTORY_LIMIT))

    @staticmethod
    def clamp_days(days: int) -> int:
        return max(1, min(days, MAX_HISTORY_DAYS))

    @staticmethod
    def parse_datetime(value: Any) -> datetime.datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime.datetime):
            dt = value
        elif isinstance(value, str):
            dt = datetime.datetime.fromisoformat(value)
        else:
            return None

        if dt.tzinfo is None:
            return dt.replace(tzinfo=datetime.UTC)
        return dt.astimezone(datetime.UTC)

    @staticmethod
    def format_count_rows(rows: Iterable[Any], *, empty: str) -> str:
        lines = []
        for index, row in enumerate(rows, start=1):
            label = str(row['label'])
            uses = int(row['uses'])
            lines.append(f'{index}. `{label}` ({formats.plural(uses):use})')
        return '\n'.join(lines) or empty

    @staticmethod
    def format_user_rows(rows: Iterable[Any], *, empty: str) -> str:
        lines = []
        for index, row in enumerate(rows, start=1):
            author_id = int(row['author_id'])
            uses = int(row['uses'])
            lines.append(f'{index}. <@{author_id}> ({formats.plural(uses):use})')
        return '\n'.join(lines) or empty

    def format_guild_rows(self, rows: Iterable[Any], *, empty: str) -> str:
        lines = []
        for index, row in enumerate(rows, start=1):
            guild_id = cast('int | None', row['guild_id'])
            uses = int(row['uses'])
            if guild_id is None:
                guild = 'Private Message'
            else:
                guild = str(self.bot.get_guild(guild_id) or f'<Unknown {guild_id}>')
            lines.append(f'{index}. {guild} ({formats.plural(uses):use})')
        return '\n'.join(lines) or empty

    async def fetch_count_and_first(
        self,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
    ) -> tuple[int, datetime.datetime | None]:
        result = await session.execute(
            select(
                func.count(col(CommandUsage.id)).label('total'),
                func.min(col(CommandUsage.used_at)).label('first_used'),
            ).where(*criteria)
        )
        row = result.mappings().one()
        return int(row['total']), self.parse_datetime(row['first_used'])

    async def fetch_top_commands(
        self,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
    ) -> list[Any]:
        command = col(CommandUsage.command)
        uses = func.count(col(CommandUsage.id)).label('uses')
        result = await session.execute(
            select(command.label('label'), uses)
            .where(*criteria)
            .group_by(command)
            .order_by(uses.desc())
            .limit(5)
        )
        return list(result.mappings().all())

    async def fetch_top_users(
        self,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
    ) -> list[Any]:
        author_id = col(CommandUsage.author_id)
        uses = func.count(col(CommandUsage.id)).label('uses')
        result = await session.execute(
            select(author_id, uses)
            .where(*criteria)
            .group_by(author_id)
            .order_by(uses.desc())
            .limit(5)
        )
        return list(result.mappings().all())

    async def fetch_top_guilds(
        self,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
    ) -> list[Any]:
        guild_id = col(CommandUsage.guild_id)
        uses = func.count(col(CommandUsage.id)).label('uses')
        result = await session.execute(
            select(guild_id, uses)
            .where(*criteria)
            .group_by(guild_id)
            .order_by(uses.desc())
            .limit(5)
        )
        return list(result.mappings().all())

    async def show_guild_stats(self, ctx: GuildContext) -> None:
        async with self.bot.db_session_maker() as session:
            total, first_used = await self.fetch_count_and_first(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
            )
            top_commands = await self.fetch_top_commands(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
            )
            top_commands_today = await self.fetch_top_commands(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.used_at)
                > discord.utils.utcnow() - datetime.timedelta(days=1),
            )
            top_users = await self.fetch_top_users(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
            )
            top_users_today = await self.fetch_top_users(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.used_at)
                > discord.utils.utcnow() - datetime.timedelta(days=1),
            )

        embed = discord.Embed(title='Server Command Stats', colour=ctx.me.colour)
        embed.description = f'{formats.plural(total):command} used.'
        embed.set_footer(text='Tracking command usage since')
        embed.timestamp = first_used or discord.utils.utcnow()
        embed.add_field(
            name='Top Commands',
            value=self.format_count_rows(top_commands, empty='No commands.'),
            inline=True,
        )
        embed.add_field(
            name='Top Commands Today',
            value=self.format_count_rows(top_commands_today, empty='No commands.'),
            inline=True,
        )
        embed.add_field(
            name='Top Command Users',
            value=self.format_user_rows(top_users, empty='No command users.'),
            inline=True,
        )
        embed.add_field(
            name='Top Command Users Today',
            value=self.format_user_rows(top_users_today, empty='No command users.'),
            inline=True,
        )
        await ctx.send(embed=embed)

    async def show_member_stats(
        self, ctx: GuildContext, member: discord.Member
    ) -> None:
        async with self.bot.db_session_maker() as session:
            total, first_used = await self.fetch_count_and_first(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.author_id) == member.id,
            )
            top_commands = await self.fetch_top_commands(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.author_id) == member.id,
            )
            top_commands_today = await self.fetch_top_commands(
                session,
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.author_id) == member.id,
                col(CommandUsage.used_at)
                > discord.utils.utcnow() - datetime.timedelta(days=1),
            )

        embed = discord.Embed(title='Command Stats', colour=member.colour)
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.description = f'{formats.plural(total):command} used.'
        embed.set_footer(text='First command used')
        embed.timestamp = first_used or discord.utils.utcnow()
        embed.add_field(
            name='Most Used Commands',
            value=self.format_count_rows(top_commands, empty='No commands.'),
            inline=False,
        )
        embed.add_field(
            name='Most Used Commands Today',
            value=self.format_count_rows(top_commands_today, empty='No commands.'),
            inline=False,
        )
        await ctx.send(embed=embed)

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
            total, _first_used = await self.fetch_count_and_first(session)
            top_commands = await self.fetch_top_commands(session)
            top_guilds = await self.fetch_top_guilds(session)
            top_users = await self.fetch_top_users(session)

        embed = discord.Embed(title='Command Stats', colour=discord.Colour.blurple())
        embed.description = f'{formats.plural(total):command} used.'
        embed.add_field(
            name='Top Commands',
            value=self.format_count_rows(top_commands, empty='No commands.'),
            inline=False,
        )
        embed.add_field(
            name='Top Guilds',
            value=self.format_guild_rows(top_guilds, empty='No guilds.'),
            inline=False,
        )
        embed.add_field(
            name='Top Users',
            value=self.format_user_rows(top_users, empty='No users.'),
            inline=False,
        )
        await ctx.send(embed=embed)

    @stats.command(name='today')
    @commands.is_owner()
    async def stats_today(self, ctx: Context) -> None:
        """Global command statistics for the last 24 hours."""
        since = discord.utils.utcnow() - datetime.timedelta(days=1)
        async with self.bot.db_session_maker() as session:
            uses = func.count(col(CommandUsage.id)).label('uses')
            state_result = await session.execute(
                select(col(CommandUsage.failed), uses)
                .where(col(CommandUsage.used_at) > since)
                .group_by(col(CommandUsage.failed))
            )
            states = list(state_result.mappings().all())
            top_commands = await self.fetch_top_commands(
                session,
                col(CommandUsage.used_at) > since,
            )
            top_guilds = await self.fetch_top_guilds(
                session,
                col(CommandUsage.used_at) > since,
            )
            top_users = await self.fetch_top_users(
                session,
                col(CommandUsage.used_at) > since,
            )

        success = 0
        failed = 0
        for row in states:
            if bool(row['failed']):
                failed += int(row['uses'])
            else:
                success += int(row['uses'])

        embed = discord.Embed(
            title='Last 24 Hour Command Stats',
            colour=discord.Colour.blurple(),
        )
        embed.description = (
            f'{formats.plural(success + failed):command} used today. '
            f'({success} succeeded, {failed} failed)'
        )
        embed.add_field(
            name='Top Commands',
            value=self.format_count_rows(top_commands, empty='No commands.'),
            inline=False,
        )
        embed.add_field(
            name='Top Guilds',
            value=self.format_guild_rows(top_guilds, empty='No guilds.'),
            inline=False,
        )
        embed.add_field(
            name='Top Users',
            value=self.format_user_rows(top_users, empty='No users.'),
            inline=False,
        )
        await ctx.send(embed=embed)

    @stats.command(name='runtime')
    async def stats_runtime(self, ctx: Context) -> None:
        """Shows runtime statistics for the current process."""
        total_members = 0
        text_channels = 0
        voice_channels = 0
        for guild in self.bot.guilds:
            total_members += guild.member_count or 0
            for channel in guild.channels:
                if isinstance(channel, discord.TextChannel):
                    text_channels += 1
                elif isinstance(channel, discord.VoiceChannel):
                    voice_channels += 1

        memory_usage = self.process.memory_full_info().uss / 1024**2
        cpu_count = psutil.cpu_count() or 1
        cpu_usage = self.process.cpu_percent() / cpu_count
        uptime = time.human_timedelta(
            self.bot.started_at,
            accuracy=None,
            brief=True,
            suffix=False,
        )

        embed = discord.Embed(title='Runtime Stats', colour=discord.Colour.blurple())
        embed.add_field(
            name='Members',
            value=f'{total_members} total\n{len(self.bot.users)} unique',
        )
        embed.add_field(
            name='Channels',
            value=(
                f'{text_channels + voice_channels} total\n'
                f'{text_channels} text\n'
                f'{voice_channels} voice'
            ),
        )
        embed.add_field(
            name='Process',
            value=f'{memory_usage:.2f} MiB\n{cpu_usage:.2f}% CPU',
        )
        embed.add_field(name='Guilds', value=str(len(self.bot.guilds)))
        embed.add_field(
            name='Commands Run', value=str(sum(self.command_stats.values()))
        )
        embed.add_field(
            name='Socket Events', value=str(sum(self.socket_stats.values()))
        )
        embed.add_field(name='Uptime', value=uptime)
        embed.set_footer(
            text=(
                f'Python {platform.python_version()} | '
                f'discord.py {version("discord.py")}'
            )
        )
        embed.timestamp = discord.utils.utcnow()
        await ctx.send(embed=embed)

    @stats.command(name='session')
    @commands.is_owner()
    async def stats_session(self, ctx: Context, limit: int = 12) -> None:
        """Shows current-process command statistics."""
        limit = self.clamp_limit(limit)
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

        await ctx.send(
            f'**{title}**\n'
            f'{total} total commands used '
            f'({slash_commands} slash command uses, {cpm:.2f}/minute)\n'
            f'```\n{table}\n```'
        )

    async def can_view_history(self, ctx: GuildContext) -> bool:
        if await self.bot.is_owner(ctx.author):
            return True

        permissions = ctx.channel.permissions_for(ctx.author)
        return ctx.author.guild_permissions.manage_guild or permissions.manage_messages

    async def require_history_access(self, ctx: GuildContext) -> None:
        if not await self.can_view_history(ctx):
            msg = 'You need Manage Server or Manage Messages to view command history.'
            raise commands.CheckFailure(msg)

    async def send_history_table(
        self,
        ctx: Context,
        rows: Iterable[CommandUsage],
        *,
        title: str,
    ) -> None:
        table = formats.TabularData()
        table.set_columns(['Command', 'Used', 'Author', 'Guild', 'Failed'])
        rendered_rows = []
        for command_usage in rows:
            used_at = self.parse_datetime(command_usage.used_at)
            used = used_at.strftime('%Y-%m-%d %H:%M') if used_at else 'Unknown'
            guild_id = command_usage.guild_id
            guild = 'DM' if guild_id is None else str(guild_id)
            rendered_rows.append(
                [
                    command_usage.command,
                    used,
                    str(command_usage.author_id),
                    guild,
                    'yes' if command_usage.failed else 'no',
                ]
            )

        if not rendered_rows:
            await ctx.send('No results found.')
            return

        table.add_rows(rendered_rows)
        output = f'**{title}**\n```\n{table.render()}\n```'
        await ctx.send(output)

    async def fetch_history(
        self,
        *,
        limit: int,
        criteria: Iterable[ColumnElement[bool]] = (),
    ) -> list[CommandUsage]:
        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(CommandUsage)
                .where(*criteria)
                .order_by(col(CommandUsage.used_at).desc())
                .limit(self.clamp_limit(limit))
            )
            return list(result.scalars().all())

    @stats.group(name='history', invoke_without_command=True)
    @commands.guild_only()
    async def stats_history(self, ctx: GuildContext, limit: int = 15) -> None:
        """Shows recent command history for this server."""
        await self.require_history_access(ctx)
        rows = await self.fetch_history(
            limit=limit,
            criteria=(col(CommandUsage.guild_id) == ctx.guild.id,),
        )
        await self.send_history_table(ctx, rows, title='Recent Server Commands')

    @stats_history.command(name='member')
    async def stats_history_member(
        self,
        ctx: GuildContext,
        member: discord.Member,
        limit: int = 15,
    ) -> None:
        """Shows recent command history for a member in this server."""
        await self.require_history_access(ctx)
        rows = await self.fetch_history(
            limit=limit,
            criteria=(
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.author_id) == member.id,
            ),
        )
        await self.send_history_table(ctx, rows, title=f'Recent Commands: {member}')

    @stats_history.command(name='command')
    async def stats_history_command(
        self,
        ctx: GuildContext,
        command: str,
        days: int = 7,
    ) -> None:
        """Shows recent command history for a command in this server."""
        await self.require_history_access(ctx)
        days = self.clamp_days(days)
        since = discord.utils.utcnow() - datetime.timedelta(days=days)
        rows = await self.fetch_history(
            limit=MAX_HISTORY_LIMIT,
            criteria=(
                col(CommandUsage.guild_id) == ctx.guild.id,
                col(CommandUsage.command) == command,
                col(CommandUsage.used_at) > since,
            ),
        )

        success = sum(not command_usage.failed for command_usage in rows)
        failed = len(rows) - success
        await ctx.send(
            f'`{command}` in the last {formats.plural(days):day}: '
            f'{success} succeeded, {failed} failed.'
        )
        await self.send_history_table(ctx, rows, title=f'Recent `{command}` Commands')

    @stats_history.command(name='global')
    @commands.is_owner()
    async def stats_history_global(self, ctx: Context, limit: int = 15) -> None:
        """Shows recent command history across every guild."""
        rows = await self.fetch_history(limit=limit)
        await self.send_history_table(ctx, rows, title='Recent Global Commands')


async def setup(client: MoistBot) -> None:
    await client.add_cog(Stats(client))

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, menus
from sqlmodel import col

from moist_bot.models import HoneypotIncident
from moist_bot.services.honeypot import HoneypotManager
from moist_bot.utils import formats
from moist_bot.utils.converters import normalize_datetime, shorten
from moist_bot.utils.formats import plural
from moist_bot.utils.paginator import RoboPages

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.sql.elements import ColumnElement

    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import GuildContext


INCIDENT_CONTENT_WIDTH = 36
INCIDENT_PAGE_SIZE = 8
INCIDENT_PAGE_SIZE_MAX = 15


async def can_manage_honeypot(ctx: GuildContext) -> bool:
    """Return whether a user can manage honeypot settings for this guild."""

    if await ctx.bot.is_owner(ctx.author):
        return True

    if ctx.author.guild_permissions.manage_guild:
        return True

    raise commands.MissingPermissions(['manage_guild'])


class HoneypotIncidentFlags(
    commands.FlagConverter, prefix='--', delimiter=' ', case_insensitive=True
):
    """Flags accepted by the incident history command."""

    limit: commands.Range[int, 1, INCIDENT_PAGE_SIZE_MAX] = commands.flag(
        default=INCIDENT_PAGE_SIZE,
        description=f'Incidents per page (1-{INCIDENT_PAGE_SIZE_MAX})',
    )


@dataclass(frozen=True, slots=True)
class HoneypotIncidentPage:
    """One lazily loaded page of honeypot incidents."""

    page_number: int
    rows: list[HoneypotIncident]


def format_incident_table(
    rows: Iterable[HoneypotIncident],
    *,
    start_index: int,
    include_user: bool,
) -> str:
    """Render incident rows as a compact text table."""

    table = formats.TabularData()

    columns = ['#', 'Triggered', 'Softban', 'Del Sec', 'Count']
    if include_user:
        columns.append('User')
    columns.append('Content')
    table.set_columns(columns)

    rendered_rows: list[list[str]] = []
    for index, incident in enumerate(rows, start=start_index):
        triggered_at = normalize_datetime(incident.triggered_at)
        triggered = (
            triggered_at.strftime('%Y-%m-%d %H:%M') if triggered_at else 'Unknown'
        )
        content = incident.content_excerpt or ''
        row = [
            str(index),
            triggered,
            'yes' if incident.softbanned else 'no',
            str(incident.delete_message_seconds),
            str(incident.trigger_count),
        ]
        if include_user:
            row.append(str(incident.user_id))
        row.append(shorten(content.replace('\n', ' '), INCIDENT_CONTENT_WIDTH))
        rendered_rows.append(row)

    table.add_rows(rendered_rows)
    return table.render()


class HoneypotIncidentPageSource(menus.PageSource):
    """Lazy paginator source for honeypot incident history."""

    def __init__(
        self,
        bot: MoistBot,
        *,
        title: str,
        criteria: Iterable[ColumnElement[bool]],
        include_user: bool,
        per_page: int,
    ) -> None:
        self.bot: MoistBot = bot
        self.title: str = title
        self.criteria: tuple[ColumnElement[bool], ...] = tuple(criteria)
        self.include_user: bool = include_user
        self.per_page: int = per_page
        self.total_entries: int = 0

    async def prepare(self) -> None:
        """Count matching incidents before the first page is shown."""

        # Keep prepare cheap by counting only matching rows
        async with self.bot.db_session_maker() as session:
            self.total_entries = await HoneypotIncident.history_count(
                session,
                criteria=self.criteria,
            )

    def is_paginating(self) -> bool:
        """Return whether controls are needed for the current result set."""

        return self.total_entries > self.per_page

    def get_max_pages(self) -> int:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Return the maximum number of available pages."""

        if self.total_entries == 0:
            return 1
        return (self.total_entries + self.per_page - 1) // self.per_page

    async def get_page(self, page_number: int) -> HoneypotIncidentPage:
        """Fetch one page of incidents from the database."""

        offset = page_number * self.per_page

        # Fetch only the visible page instead of materializing all incidents
        async with self.bot.db_session_maker() as session:
            rows = await HoneypotIncident.history(
                session,
                limit=self.per_page,
                offset=offset,
                criteria=self.criteria,
            )

        if not rows and page_number > 0:
            raise IndexError
        return HoneypotIncidentPage(page_number=page_number, rows=rows)

    async def format_page(
        self,
        menu: RoboPages,
        page: HoneypotIncidentPage,
    ) -> str:
        """Format one loaded incident page for Discord."""

        lines = [f'**{self.title}**']
        if not page.rows:
            lines.append('No honeypot incidents found.')
            return '\n'.join(lines)

        table = format_incident_table(
            page.rows,
            start_index=(page.page_number * self.per_page) + 1,
            include_user=self.include_user,
        )
        lines.append(f'```\n{table}\n```')

        maximum = self.get_max_pages()
        if maximum > 1:
            lines.append(
                f'Page {menu.current_page + 1}/{maximum} '
                f'({plural(self.total_entries):incident})'
            )

        return '\n'.join(lines)


async def send_incident_paginator(
    ctx: GuildContext,
    bot: MoistBot,
    *,
    title: str,
    criteria: Iterable[ColumnElement[bool]],
    include_user: bool,
    per_page: int,
) -> None:
    """Create and send a lazy incident paginator."""

    source = HoneypotIncidentPageSource(
        bot,
        title=title,
        criteria=criteria,
        include_user=include_user,
        per_page=per_page,
    )
    await source._prepare_once()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    pages = RoboPages(source, ctx=ctx, check_embeds=False)
    await pages.start()


class Honeypot(commands.Cog):
    """Honeypot moderation commands and message listener."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{HONEY POT}')

    async def cog_check(self, ctx: GuildContext) -> bool:  # type: ignore[override]
        if ctx.guild is None:  # type: ignore[unreachable]
            raise commands.NoPrivateMessage
        return await can_manage_honeypot(ctx)

    async def cog_load(self) -> None:
        await self.bot.honeypot.load()

    async def cog_unload(self) -> None:
        self.bot.honeypot.cancel_startup_scan()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Start the one-time honeypot startup scan after the bot is ready."""

        self.bot.honeypot.start_startup_scan()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.bot.honeypot.handle_message(message)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.bot.honeypot.delete_config(guild_id=guild.id)

    @commands.group(name='honeypot', invoke_without_command=True)
    @commands.guild_only()
    @commands.check(can_manage_honeypot)
    async def honeypot(self, ctx: GuildContext) -> None:
        """Manage this server's honeypot."""
        await ctx.send_help(ctx.command)

    @honeypot.command(name='set')
    async def honeypot_set(
        self,
        ctx: GuildContext,
        honeypot_channel: discord.TextChannel,
        log_channel: discord.TextChannel,
    ) -> None:
        """Configure this server's honeypot channel and log channel."""

        if honeypot_channel.id == log_channel.id:
            await ctx.reply(':warning: Honeypot and log channel must be different.')
            return

        config = await self.bot.honeypot.set_config(
            guild_id=ctx.guild.id,
            channel_id=honeypot_channel.id,
            log_channel_id=log_channel.id,
            updated_by_id=ctx.author.id,
        )

        # Make warnings
        warnings: list[str] = []
        honeypot_perms = honeypot_channel.permissions_for(ctx.me)
        log_perms = log_channel.permissions_for(ctx.me)
        if not ctx.me.guild_permissions.ban_members:
            warnings.append('I am missing **Ban Members**.')
        if not honeypot_perms.read_message_history:
            warnings.append(
                f'I cannot read message history in {honeypot_channel.mention}.'
            )
        if not honeypot_perms.manage_messages:
            warnings.append(f'I cannot manually clean {honeypot_channel.mention}.')
        if not log_perms.send_messages:
            warnings.append(f'I cannot send messages in {log_channel.mention}.')
        if not log_perms.embed_links:
            warnings.append(f'I cannot send embeds in {log_channel.mention}.')

        lines = [
            ':white_check_mark: Honeypot enabled.',
            f'Honeypot: {honeypot_channel.mention}',
            f'Logs: {log_channel.mention}',
            f'Config ID: `{config.id}`',
        ]
        if warnings:
            lines.append('')
            lines.extend(f':warning: {warning}' for warning in warnings)

        # Finally, send the reply
        await ctx.reply('\n'.join(lines))

    @honeypot.command(name='disable')
    async def honeypot_disable(self, ctx: GuildContext) -> None:
        """Disable this server's honeypot."""

        disabled = await self.bot.honeypot.disable_config(
            guild_id=ctx.guild.id,
            updated_by_id=ctx.author.id,
        )
        if not disabled:
            await ctx.reply(
                ':warning: This server does not have a honeypot configured.'
            )
            return

        await ctx.reply(':white_check_mark: Honeypot disabled.')

    @honeypot.command(name='show')
    async def honeypot_show(self, ctx: GuildContext) -> None:
        """Show this server's honeypot config."""

        config = self.bot.honeypot.get_config(ctx.guild.id)
        if config is None:
            await ctx.reply(
                ':warning: This server does not have a honeypot configured.'
            )
            return

        state = 'enabled' if config.enabled else 'disabled'
        count = await self.bot.honeypot.incident_count_for_guild(guild_id=ctx.guild.id)
        await ctx.reply(
            f'Honeypot is **{state}**.\n'
            f'Honeypot: <#{config.channel_id}>\n'
            f'Logs: <#{config.log_channel_id}>\n'
            f'Total incidents: `{count}`'
        )

    @honeypot.command(name='history', aliases=['logs', 'incidents'])
    async def honeypot_history(
        self,
        ctx: GuildContext,
        user: discord.Member | None = None,
        *,
        flags: HoneypotIncidentFlags,
    ) -> None:
        """Show paginated honeypot incidents."""

        criteria = [col(HoneypotIncident.guild_id) == ctx.guild.id]
        title = 'Honeypot Incidents'
        include_user = True

        if user is not None:
            criteria.append(col(HoneypotIncident.user_id) == user.id)
            title = f'Honeypot Incidents for {user}'
            include_user = False

        await send_incident_paginator(
            ctx,
            self.bot,
            title=title,
            criteria=criteria,
            include_user=include_user,
            per_page=flags.limit,
        )


async def setup(bot: MoistBot) -> None:
    bot.honeypot = HoneypotManager(bot)
    await bot.add_cog(Honeypot(bot))

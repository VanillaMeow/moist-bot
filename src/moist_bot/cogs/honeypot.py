from __future__ import annotations

import importlib
from dataclasses import dataclass
from inspect import cleandoc
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, menus
from sqlmodel import col

from moist_bot.models import HoneypotIncident
from moist_bot.services import honeypot as honeypot_service
from moist_bot.settings import settings
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

UD = '\N{VARIATION SELECTOR-16}'  # Discord unicode variation
ALERT = (
    '<a:alert:1509312153713250314>'
    if settings.use_fleabot
    else '<a:alert:1509313613284769833>'
)

HONEYPOT_ALERT_CONTENT = cleandoc(f"""
# {ALERT}
# \N{WARNING SIGN}{UD} DO NOT SEND ANY MESSAGES HERE. YOU WILL BE __IRREVERSIBLY BANNED.__ \N{HAMMER}

\N{NO ENTRY SIGN} THIS IS A TRAP FOR COMPROMISED ACCOUNTS.
\N{INFORMATION SOURCE}{UD} Messages posted here will be **automatically** deleted, and the sender will be **automatically** banned.

**YOU HAVE BEEN WARNED. INTENTIONALLY SENDING MESSAGES WILL GET YOU BANNED WITH NO APPEALS.**
# {ALERT}
""")


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


class HoneypotSendFlags(
    commands.FlagConverter, prefix='--', delimiter=' ', case_insensitive=True
):
    """Flags accepted by the alert send command."""

    force_new: bool = commands.flag(
        name='force',
        default=False,
        description='Send a new alert message instead of editing the stored one',
    )


class HoneypotAlertEmbed(discord.Embed):
    """Default honeypot alert message embed."""

    def __init__(self) -> None:
        super().__init__(
            title='\N{HONEY POT} Honeypot Alert',
            description=HONEYPOT_ALERT_CONTENT,
            colour=discord.Colour.gold(),
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
        triggered = triggered_at.strftime('%Y-%m-%d %H:%M')
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


async def edit_honeypot_alert_message(
    ctx: GuildContext,
    channel: discord.TextChannel,
    message_id: int,
) -> tuple[discord.Message | None, bool]:
    """Edit an existing alert message or report why it cannot be edited."""

    try:
        message = await ctx.bot.get_or_fetch_message(channel, message_id)
    except discord.NotFound:
        return None, True
    except discord.HTTPException:
        await ctx.reply(':warning: I cannot fetch the stored alert message.')
        return None, False

    try:
        await message.edit(
            content=HONEYPOT_ALERT_CONTENT,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        await ctx.reply(':warning: I cannot edit the stored alert message.')
        return None, False

    return message, True


async def send_honeypot_alert_message(
    ctx: GuildContext,
    channel: discord.TextChannel,
) -> discord.Message | None:
    """Send a new honeypot alert message."""

    try:
        return await channel.send(
            content=HONEYPOT_ALERT_CONTENT,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        await ctx.reply(':warning: I cannot send messages in the honeypot channel.')
        return None


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

    @honeypot.command(name='send')
    async def honeypot_send(
        self,
        ctx: GuildContext,
        *,
        flags: HoneypotSendFlags,
    ) -> None:
        """Send or update the configured honeypot alert message."""

        config = self.bot.honeypot.get_config(ctx.guild.id)
        if config is None:
            await ctx.reply(
                ':warning: This server does not have a honeypot configured.'
            )
            return

        channel = ctx.guild.get_channel(config.channel_id)
        if not isinstance(channel, discord.TextChannel):
            await ctx.reply(
                ':warning: Configured honeypot channel is not a text channel.'
            )
            return

        # Send the alert message

        if config.alert_message_id is not None and not flags.force_new:
            message, can_continue = await edit_honeypot_alert_message(
                ctx, channel, config.alert_message_id
            )
            if message is not None:
                await self.bot.honeypot.set_alert_message_id(
                    guild_id=ctx.guild.id,
                    alert_message_id=message.id,
                    updated_by_id=ctx.author.id,
                )
                await ctx.reply(
                    f':white_check_mark: Updated honeypot alert message: {message.jump_url}'
                )
                return
            if not can_continue:
                return

        message = await send_honeypot_alert_message(ctx, channel)
        if message is None:
            return

        await self.bot.honeypot.set_alert_message_id(
            guild_id=ctx.guild.id,
            alert_message_id=message.id,
            updated_by_id=ctx.author.id,
        )
        await ctx.reply(
            f':white_check_mark: Sent honeypot alert message: {message.jump_url}'
        )

    @honeypot.command(name='toggle', aliases=['enable', 'disable'])
    async def honeypot_toggle(self, ctx: GuildContext) -> None:
        """Toggle this server's honeypot.
        You can also use `honeypot enable` or `honeypot disable`.
        """

        config = self.bot.honeypot.get_config(ctx.guild.id)
        if config is None:
            await ctx.reply(
                ':warning: This server does not have a honeypot configured.'
            )
            return

        invoked_with = ctx.invoked_with.lower() if ctx.invoked_with else 'toggle'
        should_enable = invoked_with == 'enable' or (
            invoked_with != 'disable' and not config.enabled
        )

        if should_enable:
            await self.bot.honeypot.enable_config(
                guild_id=ctx.guild.id,
                updated_by_id=ctx.author.id,
            )
            action = 'enabled'
        else:
            await self.bot.honeypot.disable_config(
                guild_id=ctx.guild.id,
                updated_by_id=ctx.author.id,
            )
            action = 'disabled'

        await ctx.reply(f':white_check_mark: Honeypot {action}.')

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
        count = self.bot.honeypot.incident_count_for_guild(guild_id=ctx.guild.id)
        alert = 'not sent'
        if config.alert_message_id is not None:
            alert = (
                f'https://discord.com/channels/{ctx.guild.id}/'
                f'{config.channel_id}/{config.alert_message_id}'
            )
        await ctx.reply(
            f'Honeypot is **{state}**.\n'
            f'Honeypot: <#{config.channel_id}>\n'
            f'Logs: <#{config.log_channel_id}>\n'
            f'Alert: {alert}\n'
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
    if bot.is_ready():
        manager_module = importlib.reload(honeypot_service)
        bot.honeypot = manager_module.HoneypotManager(bot)
        bot.honeypot.mark_startup_scan_done()
        await bot.honeypot.load()

    await bot.add_cog(Honeypot(bot))

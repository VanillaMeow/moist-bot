"""Configure the bot-owned spam service through guild moderation commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import discord
from discord.ext import commands
from pydantic import ValidationError

from moist_bot.services.spam.types import (
    ExemptionKind,
    SpamRule,
)

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context, GuildContext


class SpamSettingFlags(commands.FlagConverter, prefix='--', delimiter=' '):
    """Parse optional general settings for the ``spam settings`` command.

    Omitted flags remain ``None`` so partial updates preserve existing settings.
    Timeout and cooldown values are in seconds; cleanup controls burst deletion.
    """

    action: Literal['alert', 'timeout', 'ban'] | None = commands.flag(default=None)
    timeout_seconds: int | None = commands.flag(name='timeout', default=None)
    delete_burst: bool | None = commands.flag(name='cleanup', default=None)
    action_cooldown: int | None = commands.flag(name='cooldown', default=None)
    log_channel: discord.TextChannel | None = commands.flag(name='log', default=None)


class SpamRuleFlags(commands.FlagConverter, prefix='--', delimiter=' '):
    """Parse a rule's enable switch, message allowance, and period in seconds."""

    enabled: bool | None = commands.flag(default=None)
    count: int | None = commands.flag(default=None)
    period: float | None = commands.flag(default=None)


class Spam(commands.Cog):
    """Various spam moderation commands."""

    def __init__(self, bot: MoistBot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[reportIncompatibleMethodOverride]
        """Require a guild member with Manage Guild or bot ownership.

        Returns
        -------
        bool
            True when the author may use this cog's commands.

        Raises
        ------
        commands.NoPrivateMessage
            The invocation does not come from a guild member.
        commands.MissingPermissions
            The author lacks Manage Guild and is not a bot owner.
        """

        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            raise commands.NoPrivateMessage

        if (
            await self.bot.is_owner(ctx.author)
            or ctx.author.guild_permissions.manage_guild
        ):
            return True

        raise commands.MissingPermissions(['manage_guild'])

    async def update_config(
        self,
        ctx: GuildContext,
        *,
        enabled: bool | None = None,
        changes: dict[str, object] | None = None,
        rule: SpamRule | None = None,
    ) -> None:
        """Save a partial configuration update and format validation failures.

        Parameters
        ----------
        ctx : GuildContext
            Invocation identifying the guild and the user making the change.
        enabled : bool or None, optional
            Requested enabled state, or ``None`` to leave it unchanged.
        changes : dict[str, object] or None, optional
            Supplied settings merged by the service under its guild lock.
        rule : SpamRule or None, optional
            Nested rule to update, or ``None`` for general enforcement settings.

        Raises
        ------
        commands.BadArgument
            Supplied settings or required bot permissions are invalid.
        """

        # Translate service errors into the bot's existing command error flow
        try:
            await self.bot.spam.config.update(
                ctx.guild,
                updated_by_id=ctx.author.id,
                enabled=enabled,
                changes=changes,
                rule=rule,
            )
        except ValidationError as exc:
            details = '; '.join(
                f'{".".join(str(part) for part in item["loc"])}: {item["msg"]}'
                for item in exc.errors()
            )
            error = f'Invalid spam settings: {details}'
            raise commands.BadArgument(error) from exc
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc

    @commands.group(name='spam', invoke_without_command=True)
    async def spam(self, ctx: GuildContext) -> None:
        """Show the server's spam settings."""

        await self.show_config(ctx)

    async def show_config(self, ctx: GuildContext) -> None:
        """Reply with the cached action settings, rule allowances, and enabled state."""

        config = self.bot.spam.config.get(ctx.guild.id)
        settings = config.settings

        # General enforcement settings come before individual rule configuration
        lines = [
            f'Spam checker: {"enabled" if config.enabled else "disabled"}',
            f'Action: {settings.action.value}',
            f'Timeout: {settings.timeout_seconds}s',
            f'Delete burst: {settings.delete_burst} (up to 100 tracked messages)',
            f'Action cooldown: {settings.action_cooldown}s',
            f'Log channel: <#{settings.log_channel_id}>'
            if settings.log_channel_id
            else 'Log channel: unset',
        ]

        for rule in SpamRule:
            rate = settings.rule(rule)
            lines.append(
                f'{rule.value}: {"enabled" if rate.enabled else "disabled"}, {rate.count}/{rate.period:g}s'
            )

        lines.extend(
            [
                f'Explicit exemptions: {len(config.exemptions)}',
                'Mention spam is handled by Discord AutoMod. No join flagging.',
            ]
        )

        await ctx.reply(
            '\n'.join(lines), allowed_mentions=discord.AllowedMentions.none()
        )

    @spam.command(name='show')
    async def spam_show(self, ctx: GuildContext) -> None:
        """Show this server's spam configuration."""

        await self.show_config(ctx)

    @spam.command(name='enable')
    async def spam_enable(self, ctx: GuildContext) -> None:
        """Enable spam checks after verifying the configured permissions."""

        await self.update_config(ctx, enabled=True)

        await ctx.reply('Spam checker enabled.')

    @spam.command(name='disable')
    async def spam_disable(self, ctx: GuildContext) -> None:
        """Disable spam checks and clear detection state, preserving settings."""

        await self.update_config(ctx, enabled=False)

        await ctx.reply('Spam checker disabled. Detection state cleared.')

    @spam.command(name='settings')
    async def spam_settings(
        self, ctx: GuildContext, *, flags: SpamSettingFlags
    ) -> None:
        """Update general enforcement settings using the supplied flags."""

        # Preserve explicit false values while discarding flags the user omitted
        changes: dict[str, object] = {
            'action': flags.action,
            'timeout_seconds': flags.timeout_seconds,
            'delete_burst': flags.delete_burst,
            'action_cooldown': flags.action_cooldown,
            'log_channel_id': flags.log_channel.id
            if flags.log_channel is not None
            else None,
        }
        supplied = {key: value for key, value in changes.items() if value is not None}
        if not supplied:
            await self.show_config(ctx)
            return

        if flags.log_channel is not None and flags.log_channel.guild.id != ctx.guild.id:
            raise commands.BadArgument('The log channel must belong to this server')

        await self.update_config(ctx, changes=supplied)
        await self.show_config(ctx)

    @spam.command(name='rule')
    async def spam_rule(
        self,
        ctx: GuildContext,
        rule: Literal['user', 'content', 'new-user'],
        *,
        flags: SpamRuleFlags,
    ) -> None:
        """Update one rule's enable switch, allowance, or replenishment period."""

        name = SpamRule(rule)
        changes: dict[str, object] = {
            'enabled': flags.enabled,
            'count': flags.count,
            'period': flags.period,
        }
        supplied = {key: value for key, value in changes.items() if value is not None}

        if not supplied:
            await self.show_config(ctx)
            return

        await self.update_config(ctx, changes=supplied, rule=name)
        await self.show_config(ctx)

    @spam.group(name='exempt', invoke_without_command=True)
    async def spam_exempt(self, ctx: GuildContext) -> None:
        """Show explicit user, role, and channel exemptions."""

        await self.show_exemptions(ctx)

    async def show_exemptions(self, ctx: GuildContext) -> None:
        """Reply with explicit exemption IDs or explain the built-in exemptions."""

        entries = sorted(self.bot.spam.config.get(ctx.guild.id).exemptions)
        if not entries:
            await ctx.reply(
                'No explicit exemptions. Owners, administrators, Manage Messages, bots, and webhooks are always exempt.'
            )
            return

        paginator = commands.Paginator(prefix='', suffix='')
        for kind, entity_id in entries:
            paginator.add_line(f'{kind.value}: {entity_id}')

        for page in paginator.pages:
            await ctx.reply(page, allowed_mentions=discord.AllowedMentions.none())

    @spam_exempt.command(name='list')
    async def spam_exempt_list(self, ctx: GuildContext) -> None:
        """List the configured exemptions."""

        await self.show_exemptions(ctx)

    @spam_exempt.command(name='add')
    async def spam_exempt_add(
        self,
        ctx: GuildContext,
        kind: Literal['user', 'role', 'channel'],
        *,
        entity: str,
    ) -> None:
        """Resolve a Discord entity and add it to the guild's exemptions."""

        # Resolve the entity within this guild before writing its exemption
        exemption_kind = ExemptionKind(kind)
        if exemption_kind == ExemptionKind.USER:
            target = await commands.MemberConverter().convert(ctx, entity)
            entity_id = target.id
        elif exemption_kind == ExemptionKind.ROLE:
            role = await commands.RoleConverter().convert(ctx, entity)
            entity_id = role.id
        else:
            try:
                channel = await commands.GuildChannelConverter().convert(ctx, entity)
            except commands.ChannelNotFound:
                thread = await commands.ThreadConverter().convert(ctx, entity)
                entity_id = thread.id
            else:
                entity_id = channel.id

        # The service clears counters containing the previously non-exempt contributions
        await self.bot.spam.config.exempt(
            ctx.guild.id,
            exemption_kind,
            entity_id,
            add=True,
            updated_by_id=ctx.author.id,
        )

        await ctx.reply(f'Exempted {kind} {entity_id}. Detection state cleared.')

    @spam_exempt.command(name='remove')
    async def spam_exempt_remove(
        self,
        ctx: GuildContext,
        kind: Literal['user', 'role', 'channel'],
        entity_id: commands.Range[int, 1, 18446744073709551615],
    ) -> None:
        """Remove an explicit exemption using the ID shown by ``spam exempt list``."""

        await self.bot.spam.config.exempt(
            ctx.guild.id,
            ExemptionKind(kind),
            entity_id,
            add=False,
            updated_by_id=ctx.author.id,
        )

        await ctx.reply(
            f'Removed exemption for {kind} {entity_id}. Detection state cleared.'
        )

    @spam.command(name='incidents')
    async def spam_incidents(
        self, ctx: GuildContext, limit: commands.Range[int, 1, 50] = 10
    ) -> None:
        """Display recent detections with separate punishment and cleanup outcomes."""

        incidents = await self.bot.spam.incidents(ctx.guild.id, limit)
        if not incidents:
            await ctx.reply('No spam incidents recorded.')
            return

        # Paginate results without allowing saved errors to ping members or roles
        paginator = commands.Paginator(prefix='', suffix='')
        for incident in incidents:
            paginator.add_line(
                f'#{incident.id} | user {incident.user_id} | {incident.rule} | '
                f'{incident.action}: {incident.status} | deleted {incident.messages_deleted}'
            )
            if incident.error:
                paginator.add_line(f'Action error: {incident.error[:500]}')
            if incident.cleanup_error:
                paginator.add_line(f'Cleanup error: {incident.cleanup_error[:500]}')

        for page in paginator.pages:
            await ctx.reply(page, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: MoistBot) -> None:
    """Register configuration commands without replacing the bot-owned service."""

    await bot.add_cog(Spam(bot))

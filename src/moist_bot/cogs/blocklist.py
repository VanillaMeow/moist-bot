# ruff: noqa: PLR0904

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from jishaku.paginators import PaginatorInterface, WrappedPaginator

from moist_bot.models import (
    BlocklistEntry,
    BlocklistScope,
    ChannelPolicyMode,
)
from moist_bot.services import blocklist as blocklist_service

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context, GuildContext


log = logging.getLogger('discord.' + __name__)


async def can_manage_guild_blocklist(ctx: Context) -> bool:
    """Return whether a user can manage blocklist rules for this guild."""

    if await ctx.bot.is_owner(ctx.author):
        return True

    if (
        ctx.guild is not None
        and isinstance(ctx.author, discord.Member)
        and ctx.author.guild_permissions.manage_guild
    ):
        return True

    raise commands.MissingPermissions(['manage_guild'])


def format_entry(entry: BlocklistEntry) -> str:
    """Format one blocklist entry for command output."""

    reason = entry.reason or 'No reason provided'
    if entry.scope == BlocklistScope.GLOBAL_USER:
        target = f'user `{entry.user_id}`'
    elif entry.scope == BlocklistScope.GUILD_USER:
        target = f'user `{entry.user_id}` in guild `{entry.guild_id}`'
    else:
        target = f'guild `{entry.guild_id}`'

    return f'- {target} ({entry.source}): {reason}'


def format_bulk_action(action: str, created: int, updated: int, noun: str) -> str:
    """Format a bulk mutation summary for command output."""

    parts: list[str] = []
    if created:
        parts.append(f'added {created}')
    if updated:
        parts.append(f'updated {updated}')

    if not parts:
        return f'{action} 0 {noun}'

    return f'{action} {" and ".join(parts)} {noun}'


class Blocklist(commands.Cog):
    """Blocklist management commands."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{NO ENTRY SIGN}')

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Leave fully blocklisted guilds and rely on implicit locked policy."""

        if self.bot.blocklist.is_guild_blocklisted(guild.id):
            log.warning(
                f'Leaving blocklisted guild {guild} ({guild.id}) after guild join.'
            )
            try:
                await guild.leave()
            except discord.HTTPException:
                log.exception(
                    f'Failed to leave blocklisted guild {guild} ({guild.id}).'
                )
            return

        if guild.id not in self.bot.blocklist.channel_policies:
            log.info(
                f'Joined guild {guild} ({guild.id}) without saved command policy; '
                'it is implicitly locked.'
            )

    async def _send_paginated_lines(
        self,
        ctx: Context,
        *,
        title: str,
        lines: list[str],
        empty: str,
    ) -> None:
        if not lines:
            await ctx.reply(empty)
            return

        paginator = WrappedPaginator(
            prefix='',
            suffix='',
            max_size=1900,
            force_wrap=True,
        )
        paginator.add_line(title)
        for line in lines:
            if not line:
                paginator.add_line(empty=True)
                continue
            paginator.add_line(line)

        interface = PaginatorInterface(ctx.bot, paginator, owner=ctx.author)
        await interface.send_to(ctx)

    def _validate_permissions(self, permissions: list[str]) -> list[str] | None:
        if not permissions:
            return None
        return self.bot.blocklist.validate_permission_names(permissions)

    @commands.group(name='blocklist', hidden=True)
    async def blocklist(self, ctx: Context) -> None:
        """Manage bot blocklists."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @blocklist.group(name='global')
    @commands.is_owner()
    async def blocklist_global(self, ctx: Context) -> None:
        """Manage global user blocklists."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @blocklist_global.command(name='add')
    async def blocklist_global_add(
        self,
        ctx: Context,
        users: commands.Greedy[discord.User],
        *,
        reason: str | None = None,
    ) -> None:
        """Globally blocklist users."""
        if not users:
            await ctx.reply(
                ':warning: Usage: `blocklist global add <users...> [reason]`'
            )
            return

        created_count = 0
        updated_count = 0
        for user in users:
            created = await self.bot.blocklist.upsert_entry(
                scope=BlocklistScope.GLOBAL_USER,
                user_id=user.id,
                created_by_id=ctx.author.id,
                reason=reason,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        await ctx.reply(
            ':white_check_mark: '
            f'{format_bulk_action("Globally", created_count, updated_count, "users")}'
        )

    @blocklist_global.command(name='remove')
    async def blocklist_global_remove(
        self,
        ctx: Context,
        user: discord.User,
    ) -> None:
        """Remove a global user blocklist."""
        removed = await self.bot.blocklist.remove_entry(
            scope=BlocklistScope.GLOBAL_USER,
            user_id=user.id,
        )
        if not removed:
            await ctx.reply(':warning: That user is not globally blocklisted.')
            return

        await ctx.reply(f':white_check_mark: Removed global blocklist for `{user.id}`.')

    @blocklist_global.command(name='list')
    async def blocklist_global_list(self, ctx: Context) -> None:
        """List global user blocklists."""
        entries = await self.bot.blocklist.entries_for_scope(BlocklistScope.GLOBAL_USER)
        await self._send_paginated_lines(
            ctx,
            title='Global user blocklists',
            lines=[format_entry(entry) for entry in entries],
            empty='No blocklist entries found.',
        )

    @blocklist.group(name='guild')
    @commands.is_owner()
    async def blocklist_guild(self, ctx: Context) -> None:
        """Manage whole-guild blocklists."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @blocklist_guild.command(name='add')
    async def blocklist_guild_add(
        self,
        ctx: Context,
        guild_ids: commands.Greedy[int],
        *,
        reason: str | None = None,
    ) -> None:
        """Blocklist entire guilds."""
        if not guild_ids:
            await ctx.reply(
                ':warning: Usage: `blocklist guild add <guild_ids...> [reason]`'
            )
            return

        created_count = 0
        updated_count = 0
        left_count = 0
        skipped_count = 0
        failed_count = 0
        for guild_id in guild_ids:
            created = await self.bot.blocklist.upsert_entry(
                scope=BlocklistScope.GUILD,
                guild_id=guild_id,
                created_by_id=ctx.author.id,
                reason=reason,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                skipped_count += 1
                continue

            try:
                await guild.leave()
            except discord.HTTPException:
                failed_count += 1
                log.exception(
                    f'Failed to leave blocklisted guild {guild} ({guild.id}).'
                )
            else:
                left_count += 1

        leave_summary = f'; left {left_count} and skipped {skipped_count}'
        if failed_count:
            leave_summary = f'{leave_summary}; failed {failed_count}'

        await ctx.reply(
            ':white_check_mark: '
            f'{format_bulk_action("Guild blocklist", created_count, updated_count, "guilds")}'
            f'{leave_summary}'
        )

    @blocklist_guild.command(name='remove')
    async def blocklist_guild_remove(self, ctx: Context, guild_id: int) -> None:
        """Remove a whole-guild blocklist."""
        removed = await self.bot.blocklist.remove_entry(
            scope=BlocklistScope.GUILD,
            guild_id=guild_id,
        )
        if not removed:
            await ctx.reply(':warning: That guild is not blocklisted')
            return

        await ctx.reply(f':white_check_mark: Removed guild blocklist for `{guild_id}`')

    @blocklist_guild.command(name='list')
    async def blocklist_guild_list(self, ctx: Context) -> None:
        """List whole-guild blocklists."""
        entries = await self.bot.blocklist.entries_for_scope(BlocklistScope.GUILD)
        await self._send_paginated_lines(
            ctx,
            title='Guild blocklists',
            lines=[format_entry(entry) for entry in entries],
            empty='No blocklist entries found.',
        )

    @blocklist.group(name='member')
    @commands.guild_only()
    @commands.check(can_manage_guild_blocklist)
    async def blocklist_member(self, ctx: GuildContext) -> None:
        """Manage member blocklists in this guild."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @blocklist_member.command(name='add')
    async def blocklist_member_add(
        self,
        ctx: GuildContext,
        members: commands.Greedy[discord.Member],
        *,
        reason: str | None = None,
    ) -> None:
        """Blocklist members in this guild."""
        if not members:
            await ctx.reply(
                ':warning: Usage: `blocklist member add <members...> [reason]`'
            )
            return

        created_count = 0
        updated_count = 0
        for member in members:
            created = await self.bot.blocklist.upsert_entry(
                scope=BlocklistScope.GUILD_USER,
                guild_id=ctx.guild.id,
                user_id=member.id,
                created_by_id=ctx.author.id,
                reason=reason,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        await ctx.reply(
            ':white_check_mark: '
            f'{format_bulk_action("Guild blocklist", created_count, updated_count, "members")}'
        )

    @blocklist_member.command(name='remove')
    async def blocklist_member_remove(
        self,
        ctx: GuildContext,
        member: discord.Member,
    ) -> None:
        """Remove a member blocklist in this guild."""
        removed = await self.bot.blocklist.remove_entry(
            scope=BlocklistScope.GUILD_USER,
            guild_id=ctx.guild.id,
            user_id=member.id,
        )
        if not removed:
            await ctx.reply(':warning: That member is not blocklisted here.')
            return

        await ctx.reply(
            f':white_check_mark: Removed guild blocklist for `{member.id}`.'
        )

    @blocklist_member.command(name='list')
    async def blocklist_member_list(self, ctx: GuildContext) -> None:
        """List member blocklists in this guild."""
        entries = await self.bot.blocklist.entries_for_scope(
            BlocklistScope.GUILD_USER,
            guild_id=ctx.guild.id,
        )
        await self._send_paginated_lines(
            ctx,
            title=f'Member blocklists for guild `{ctx.guild.id}`',
            lines=[format_entry(entry) for entry in entries],
            empty='No blocklist entries found.',
        )

    @blocklist.command(name='policy')
    @commands.guild_only()
    @commands.check(can_manage_guild_blocklist)
    async def blocklist_policy(
        self,
        ctx: GuildContext,
        mode: ChannelPolicyMode,
    ) -> None:
        """Set this guild's command access policy mode."""
        await self.bot.blocklist.set_channel_mode(
            guild_id=ctx.guild.id,
            mode=mode,
            updated_by_id=ctx.author.id,
        )
        await ctx.reply(f':white_check_mark: Command policy mode set to `{mode}`.')

    @blocklist.group(name='permission')
    @commands.guild_only()
    @commands.check(can_manage_guild_blocklist)
    async def blocklist_permission(self, ctx: GuildContext) -> None:
        """Manage command policy permissions for this guild."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @blocklist_permission.command(name='add')
    async def blocklist_permission_add(
        self, ctx: GuildContext, *permissions: str
    ) -> None:
        """Add permissions to this guild's command policy."""
        try:
            permission_names = self._validate_permissions(list(permissions))
        except ValueError as e:
            await ctx.reply(f':warning: Unknown Discord permission `{e.args[0]}`.')
            return

        if permission_names is None:
            await ctx.reply(
                ':warning: Usage: `blocklist permission add <permissions...>`'
            )
            return

        created_count = 0
        existing_count = 0
        for permission_name in permission_names:
            created = await self.bot.blocklist.add_permission(
                guild_id=ctx.guild.id,
                permission_name=permission_name,
            )
            if created:
                created_count += 1
            else:
                existing_count += 1

        await ctx.reply(
            ':white_check_mark: '
            f'Policy added {created_count} and skipped {existing_count} permissions.'
        )

    @blocklist_permission.command(name='remove')
    async def blocklist_permission_remove(
        self, ctx: GuildContext, *permissions: str
    ) -> None:
        """Remove permissions from this guild's command policy."""
        try:
            permission_names = self._validate_permissions(list(permissions))
        except ValueError as e:
            await ctx.reply(f':warning: Unknown Discord permission `{e.args[0]}`.')
            return

        if permission_names is None:
            await ctx.reply(
                ':warning: Usage: `blocklist permission remove <permissions...>`.'
            )
            return

        removed_count = 0
        missing_count = 0
        for permission_name in permission_names:
            removed = await self.bot.blocklist.remove_permission(
                guild_id=ctx.guild.id,
                permission_name=permission_name,
            )
            if removed:
                removed_count += 1
            else:
                missing_count += 1

        await ctx.reply(
            ':white_check_mark: '
            f'Policy removed {removed_count} and skipped {missing_count} permissions.'
        )

    @blocklist_permission.command(name='list')
    async def blocklist_permission_list(self, ctx: GuildContext) -> None:
        """List this guild's command policy permissions."""
        permission_names = sorted(
            self.bot.blocklist.get_channel_policy(ctx.guild.id).permission_names
        )
        await self._send_paginated_lines(
            ctx,
            title=f'Command policy permissions for guild `{ctx.guild.id}`.',
            lines=[f'- `{permission_name}`' for permission_name in permission_names],
            empty='No permissions configured.',
        )

    @blocklist_permission.command(name='clear')
    async def blocklist_permission_clear(self, ctx: GuildContext) -> None:
        """Remove all permissions from this guild's command policy."""
        removed_count = await self.bot.blocklist.clear_permissions(
            guild_id=ctx.guild.id
        )
        await ctx.reply(
            f':white_check_mark: Removed {removed_count} policy permissions.'
        )

    @blocklist.group(name='channel')
    @commands.guild_only()
    @commands.check(can_manage_guild_blocklist)
    async def blocklist_channel(self, ctx: GuildContext) -> None:
        """Manage channel command policy for this guild."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @blocklist_channel.command(name='add')
    async def blocklist_channel_add(
        self,
        ctx: GuildContext,
        channels: commands.Greedy[discord.TextChannel],
    ) -> None:
        """Add channels to this guild's channel policy."""
        channel_ids = (
            [channel.id for channel in channels] if channels else [ctx.channel.id]
        )

        created_count = 0
        existing_count = 0
        for channel_id in channel_ids:
            created = await self.bot.blocklist.add_channel(
                guild_id=ctx.guild.id,
                channel_id=channel_id,
            )
            if created:
                created_count += 1
            else:
                existing_count += 1

        await ctx.reply(
            ':white_check_mark: '
            f'Policy added {created_count} and skipped {existing_count} channels.'
        )

    @blocklist_channel.command(name='remove')
    async def blocklist_channel_remove(
        self,
        ctx: GuildContext,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Remove a channel from this guild's channel policy."""
        channel_id = ctx.channel.id if channel is None else channel.id
        removed = await self.bot.blocklist.remove_channel(
            guild_id=ctx.guild.id,
            channel_id=channel_id,
        )
        if not removed:
            await ctx.reply(':warning: That channel is not configured.')
            return

        await ctx.reply(f':white_check_mark: Removed <#{channel_id}> from policy.')

    @blocklist_channel.command(name='list')
    async def blocklist_channel_list(self, ctx: GuildContext) -> None:
        """List this guild's channel policy."""
        policy = self.bot.blocklist.get_channel_policy(ctx.guild.id)
        channel_lines = [
            f'- <#{channel_id}> (`{channel_id}`)'
            for channel_id in sorted(policy.channel_ids)
        ]
        permission_lines = [
            f'- `{permission_name}`'
            for permission_name in sorted(policy.permission_names)
        ]

        lines = [
            f'Current mode: `{policy.mode}`.',
            '',
            'Channels:',
            *(channel_lines or ['No channels configured.']),
            '',
            'Permissions:',
            *(permission_lines or ['No permissions configured.']),
        ]
        await self._send_paginated_lines(
            ctx,
            title=f'Command policy for guild `{ctx.guild.id}`',
            lines=lines,
            empty='No channel policy configured.',
        )

    @blocklist_channel.command(name='clear')
    async def blocklist_channel_clear(self, ctx: GuildContext) -> None:
        """Remove all channels from this guild's command policy."""
        removed_count = await self.bot.blocklist.clear_channels(guild_id=ctx.guild.id)
        await ctx.reply(f':white_check_mark: Removed {removed_count} policy channels.')


async def setup(bot: MoistBot) -> None:
    if bot.is_ready():
        manager_module = importlib.reload(blocklist_service)
        bot.blocklist = manager_module.BlocklistManager(bot)
        await bot.blocklist.load()

    await bot.add_cog(Blocklist(bot))

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Literal
from unicodedata import name as unicodedata_name

import discord
import discord.utils
from discord.ext import commands

from moist_bot.settings import settings
from moist_bot.utils.converters import get_media_from_ctx

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context, GuildContext


log = logging.getLogger('discord.' + __name__)


# fmt: off
class StickerFlags(commands.FlagConverter, prefix='--', delimiter=' ', case_insensitive=True):
    alias: str = commands.flag(aliases=['name', 'n', 'a'])
    description: str = commands.flag(aliases=['desc', 'd'], default='No description provided.')
    related_emoji: str = commands.flag(aliases=['emoji', 'e'])
    sticker_link: str | None = commands.flag(aliases=['sticker', 'link', 's'])
# fmt: on


class OwnerDebug(commands.Cog):
    """Debug commands that only the bot owner can use."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{HAMMER AND WRENCH}')

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[reportIncompatibleMethodOverride]
        if not await ctx.bot.is_owner(ctx.author):
            raise commands.NotOwner('You do not own this bot.')
        return True

    @commands.group(hidden=True)
    async def debug(self, ctx: Context):
        pass

    @debug.command(name='copyglobal')
    async def copy_global_to_test_guild(self, ctx: Context, resync: bool | None = True):
        self.bot.tree.copy_global_to(guild=settings.test_guild)
        await ctx.reply(
            ':white_check_mark: Copied global app commands to **test guild**'
        )

        if resync:
            try:
                await self.bot.tree.sync(guild=settings.test_guild)
            except discord.DiscordException:
                log.exception('Unable to sync application commands.')
                await ctx.reply(':anger: Unable to sync application commands.')
                return

            await ctx.invoke(self.sync_app_cmds, guild='guild')  # type: ignore[]

    @debug.command(name='unloadappcmd')
    async def unload_app_cmd(self, ctx: Context, cmd: str, resync: bool | None = False):
        """Unload an application command."""
        unloaded = self.bot.tree.remove_command(cmd)

        if resync:
            try:
                await self.bot.tree.sync()
            except discord.DiscordException:
                log.exception('Unable to sync application commands.')
                await ctx.reply(':anger: Unable to sync application commands.')
                return

            await ctx.reply(f':white_check_mark: Unloaded and re-synced `{unloaded}`.')
        else:
            await ctx.reply(
                f':white_check_mark: Unloaded `{unloaded}`.\n:warning: Re-sync is required.'
            )

    @debug.command(name='syncappcmds')
    async def sync_app_cmds(
        self, ctx: Context, target: Literal['guild', 'global'] | None = 'guild'
    ):
        """Sync application commands."""

        target_fmt = ''
        guild: discord.Object | None = None

        if target == 'global':
            guild = None
            target_fmt = 'global guilds'
        elif target == 'guild':
            guild = settings.test_guild
            target_fmt = 'current guild'

        try:
            synced = await self.bot.tree.sync(guild=guild)
        except commands.CommandError:
            log.exception('Unable to sync application commands.')
            await ctx.reply(':anger: Unable to sync application commands.')
            return

        fmt = '\n'.join(repr(cmd) for cmd in synced) if synced else 'None'
        await ctx.reply(f':white_check_mark: Synced in **{target_fmt}**:\n`{fmt}`')

    @debug.command(name='getappcmds')
    async def get_app_cmds(
        self, ctx: Context, target: Literal['guild', 'global'] | None = 'guild'
    ):
        """Fetch currently registered application commands."""

        target_fmt = ''
        guild: discord.Object | None = None

        if target == 'global':
            guild = None
            target_fmt = 'global guilds'
        elif target == 'guild':
            guild = settings.test_guild
            target_fmt = 'current guild'

        cmds = await self.bot.tree.fetch_commands(guild=guild)

        fmt = '\n'.join(repr(cmd) for cmd in cmds) if cmds else 'None'
        await ctx.reply(
            f':white_check_mark: Fetched {len(cmds)} command(s) in **{target_fmt}**:\n`{fmt}`'
        )

    @debug.command()
    async def clear(self, ctx: Context):
        """Clears the console."""
        clear_cmd = 'cls' if os.name == 'nt' else 'clear'
        await asyncio.create_subprocess_shell(clear_cmd)
        await ctx.message.add_reaction('✅')
        log.info('Console cleared.')

    @commands.guild_only()
    @debug.command()
    async def give_role(
        self,
        ctx: Context,
        role: discord.Role,
        *,
        member: discord.Member = commands.Author,
    ):
        """Give someone a role."""

        try:
            await member.add_roles(role)
        except discord.Forbidden:
            await ctx.reply(':no_entry: lol I dont have the perms for that xd')
            return

        await ctx.reply(
            f':white_check_mark: Successfully given role `{role.name}` to `{member}`'
        )

    @commands.command()
    async def update_status(self, ctx: Context):
        """Update the bots status."""
        guilds = len(self.bot.guilds)
        await self.bot.change_presence(
            activity=discord.Game(f'with {guilds} moisturized servers')
        )
        await ctx.send(':white_check_mark: Status updated.')

    @commands.command(hidden=True)
    async def methods(self, ctx: Context, user: discord.Member = commands.Author):
        """Used for debugging."""

        await ctx.reply(
            f'id: {user.id}\n'
            f'Mention: {user.mention}\n'
            f'Raw: {user}\n'
            f'Nick: {user.nick}\n'
            f'Name: {user.name}\n'
            f'Display name: {user.display_name}\n'
            f'Discriminator: {user.discriminator}\n'
            f'Avatar: {user.avatar}'
        )

    @debug.group()
    async def emoji(self, _ctx: GuildContext):
        pass

    @emoji.command(name='add')
    async def emoji_add(
        self, ctx: GuildContext, alias: str, emoji_link: str | None = None
    ):
        """Create a custom Emoji for the guild."""
        reply = ctx.replied_message

        # Fetch emoji bytes
        if emoji_link:
            emoji = await self.bot.http.get_from_cdn(emoji_link)
        elif reply and reply.attachments:
            emoji = await reply.attachments[0].read(use_cached=True)
        else:
            return await ctx.reply(':warning: Missing image', ephemeral=True)

        # Format alias
        alias = alias.replace(' ', '_')

        # Create emoji
        await ctx.guild.create_custom_emoji(name=alias, image=emoji)
        await ctx.reply(f':white_check_mark: Added emoji :{alias}:')

    @emoji_add.error
    async def emoji_add_error(self, ctx: GuildContext, error: discord.DiscordException):
        error = getattr(error, 'original', error)

        if isinstance(error, discord.Forbidden):
            await ctx.reply(':no_entry: lol I dont have the perms for that xd')

        elif isinstance(error, discord.HTTPException):
            log.error('Unable to add emoji', exc_info=error.__traceback__)  # type: ignore[]
            await ctx.reply(':warning: Unable to resolve emoji')

        else:
            log.error('Unable to add emoji', exc_info=error.__traceback__)  # type: ignore[]
            await ctx.reply(":no_entry: I can't do that :(")

    @emoji.command(name='remove', aliases=['del', 'delete'])
    async def emoji_remove(self, ctx: GuildContext, alias: str):
        """Remove a custom Emoji from the guild."""

        # error handling? never heard of it :3
        emoji = discord.utils.get(ctx.guild.emojis, name=alias)
        if emoji is None:
            await ctx.reply(':no_entry: Emoji not found.')
            return

        await ctx.guild.delete_emoji(emoji)
        await ctx.reply(':white_check_mark: Deleted emoji.')

    @debug.group()
    async def sticker(self, _ctx: GuildContext):
        pass

    @sticker.command(name='add')
    async def sticker_add(self, ctx: GuildContext, *, flags: StickerFlags):
        """Create a Sticker for the guild."""

        # Fetch sticker bytes
        sticker = await get_media_from_ctx(ctx, arg=flags.sticker_link)

        # This only occurs when all checks fail
        if not sticker:
            return await ctx.reply(':warning: Missing image.', ephemeral=True)

        # Convert bytes into a file
        sticker = discord.File(fp=sticker)
        related_emoji = unicodedata_name(flags.related_emoji)

        # Create sticker
        await ctx.guild.create_sticker(
            name=flags.alias,
            description=flags.description,
            emoji=related_emoji,
            file=sticker,
        )
        await ctx.reply(f':white_check_mark: Added sticker `{flags.alias}`')

    @sticker_add.error
    async def sticker_add_error(
        self, ctx: GuildContext, error: discord.DiscordException
    ):
        error = getattr(error, 'original', error)

        if isinstance(error, discord.Forbidden):
            await ctx.reply(':no_entry: lol I dont have the perms for that xd')

        elif isinstance(error, commands.MissingRequiredFlag):
            await ctx.reply(f':warning: {error!s}')

        elif isinstance(error, discord.HTTPException):
            log.error('Unable to add sticker', exc_info=error.__traceback__)  # type: ignore[]
            await ctx.reply(':warning: Unable to resolve sticker')

        else:
            log.error('Unable to add sticker', exc_info=error.__traceback__)  # type: ignore[]
            await ctx.reply(":no_entry: I can't do that :(")

    @sticker.command(name='remove', aliases=['del', 'delete'])
    async def sticker_remove(self, ctx: GuildContext, alias: str):
        """Deletes a Sticker from the guild"""

        sticker = discord.utils.get(ctx.guild.stickers, name=alias)
        if sticker is None:
            await ctx.reply(':no_entry: Sticker not found.')
            return

        await ctx.guild.delete_sticker(sticker)
        await ctx.reply(':white_check_mark: Deleted sticker.')


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(OwnerDebug(bot))

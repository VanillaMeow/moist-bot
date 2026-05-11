# pyright: standard

from __future__ import annotations

import asyncio
import inspect
import itertools
import unicodedata
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, menus

from moist_bot.utils.paginator import RoboPages

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context, GuildContext


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

    @commands.command(name='quit', hidden=True)
    @commands.is_owner()
    async def _quit(self, _ctx: Context):
        """Quits the bot."""
        await self.bot.close()

    @staticmethod
    async def say_permissions(
        ctx: Context,
        member: discord.Member,
        channel: discord.abc.GuildChannel | discord.Thread,
    ) -> None:
        permissions = channel.permissions_for(member)
        e = discord.Embed(colour=member.colour)
        avatar = member.display_avatar.with_static_format('png')
        e.set_author(name=str(member), url=avatar)
        allowed: list[str] = []
        denied: list[str] = []
        for name, value in permissions:
            perm_name = name.replace('_', ' ').replace('guild', 'server').title()
            if value:
                allowed.append(perm_name)
            else:
                denied.append(perm_name)

        e.add_field(name='Allowed', value='\n'.join(allowed))
        e.add_field(name='Denied', value='\n'.join(denied))
        await ctx.reply(embed=e)

    @commands.command()
    @commands.is_owner()
    async def debugpermissions(
        self, ctx: Context, guild_id: int, channel_id: int, author_id: int | None = None
    ):
        """Shows permission resolution for a channel and an optional author."""

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return await ctx.reply('Guild not found?')

        channel = guild.get_channel(channel_id)
        if channel is None:
            return await ctx.reply('Channel not found?')

        if author_id is None:
            member = guild.me
        else:
            member = await guild.fetch_member(author_id)

        if member is None:  # pyright: ignore[reportUnnecessaryComparison]
            return await ctx.reply('Member not found?')

        await self.say_permissions(ctx, member, channel)

    @commands.command(hidden=True)
    @commands.cooldown(1, 30.0, type=commands.BucketType.user)
    async def cud(self, ctx: Context):
        """pls no spam"""

        for i in range(3):
            await ctx.send(str(3 - i))
            await asyncio.sleep(1)

        await ctx.send('go')


async def setup(client: MoistBot) -> None:
    await client.add_cog(Meta(client))

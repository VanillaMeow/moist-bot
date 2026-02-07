# ruff: noqa: F401, PLR0904, S102, S307
# pyright: reportUnusedImport=false, reportPrivateUsage=false

from __future__ import annotations

import asyncio
import datetime
import inspect
import io
import logging
import math
import os
import sys
import textwrap
import time
import traceback
from contextlib import redirect_stdout
from importlib.metadata import distribution, packages_distributions
from typing import TYPE_CHECKING, Any, Literal
from unicodedata import name as unicodedata_name

import discord
import discord.utils
import psutil
from discord.ext import commands
from jishaku.modules import package_version

from moist_bot.config import GUILD_OBJECT
from moist_bot.constants import COGS_FOLDER_PATH, ROOT_PACKAGE
from moist_bot.utils.converters import get_media_from_ctx

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context, GuildContext


log = logging.getLogger('discord.' + __name__)


cogs = COGS_FOLDER_PATH.name


# fmt: off
class StickerFlags(commands.FlagConverter, prefix='--', delimiter=' ', case_insensitive=True):
    alias: str = commands.flag(aliases=['name', 'n', 'a'])
    description: str = commands.flag(aliases=['desc', 'd'], default='No description provided.')
    related_emoji: str = commands.flag(aliases=['emoji', 'e'])
    sticker_link: str | None = commands.flag(aliases=['sticker', 'link', 's'])
# fmt: on


class OwnerOnly(commands.Cog):
    """Debug commands that only the bot owner can use"""

    def __init__(self, client: MoistBot):
        self.client: MoistBot = client
        self._last_result: Any = None
        self.process = psutil.Process()
        self.sessions: set[int] = set()
        self.last_ext: str = 'cmds'

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore pylint: disable=invalid-overridden-method
        if not await ctx.bot.is_owner(ctx.author):
            raise commands.NotOwner('You do not own this bot.')
        return True

    @staticmethod
    def cleanup_code(content: str) -> str:
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith('```') and content.endswith('```'):
            return '\n'.join(content.split('\n')[1:-1])

        # remove `foo`
        return content.strip('` \n')

    @staticmethod
    def get_syntax_error(e: SyntaxError) -> str:
        if e.text is None:
            return f'```py\n{e.__class__.__name__}: {e}\n```'
        return f'```py\n{e.text}{"^":>{e.offset}}\n{e.__class__.__name__}: {e}```'

    @commands.is_owner()
    @commands.command(hidden=True)
    async def reload(self, ctx: Context, ext: str | None = None):
        """Reload a cog."""

        # If not provided, use the last extension used
        if ext is None:
            ext = self.last_ext

        try:
            await self.client.reload_extension(f'.{cogs}.{ext}', package=ROOT_PACKAGE)

        except commands.ExtensionNotLoaded, commands.ExtensionNotFound:
            return await ctx.reply(":anger: specified cog name doesn't exits bozo")

        except commands.ExtensionFailed as e:
            msg = f'Reloading raised an exception: `{type(e.__class__)}`\n'
            log.exception(msg, exc_info=e.__traceback__)  # type: ignore[]
            await ctx.reply(f':anger: {msg}\n`{e}`')
            return None

        await ctx.reply(f':repeat: Reloaded {ext}.')
        self.last_ext = ext

    @commands.is_owner()
    @commands.command(hidden=True)
    async def load(self, ctx: Context, ext: str):
        """Load a cog."""

        try:
            await self.client.load_extension(f'.{cogs}.{ext}', package=ROOT_PACKAGE)

        except commands.ExtensionAlreadyLoaded, commands.ExtensionNotFound:
            await ctx.reply(
                ":anger: specified cog is already loaded or doesn't exits bozo"
            )
            return

        except commands.ExtensionFailed as e:
            msg = f'Loading raised an exception: `{type(e.__class__)}`\n'
            log.exception(msg, exc_info=e.__traceback__)  # type: ignore[]
            await ctx.reply(f':anger: {msg}\n`{e}`')
            return

        await ctx.reply(f':white_check_mark: Loaded {ext}.')

    @commands.is_owner()
    @commands.command(hidden=True)
    async def unload(self, ctx: Context, ext: str):
        """Unload a cog."""

        try:
            await self.client.unload_extension(f'.{cogs}.{ext}', package=ROOT_PACKAGE)
        except commands.ExtensionNotFound, commands.ExtensionNotLoaded:
            await ctx.reply(":anger: specified cog name doesn't exits bozo")
            return

        await ctx.reply(f':white_check_mark: Unloaded {ext}.')

    @commands.is_owner()
    @commands.group(hiddden=True)
    async def debug(self, ctx: Context):
        pass

    @debug.command(name='copyglobal')
    async def copy_global_to_test_guild(self, ctx: Context, resync: bool | None = True):
        self.client.tree.copy_global_to(guild=GUILD_OBJECT)
        await ctx.reply(
            ':white_check_mark: Copied global app commands to **test guild**'
        )

        if resync:
            try:
                await self.client.tree.sync(guild=GUILD_OBJECT)
            except discord.DiscordException:
                log.exception('Unable to sync application commands.')
                await ctx.reply(':anger: Unable to sync application commands.')
                return

            await ctx.invoke(self.sync_app_cmds, guild='guild')  # type: ignore[]

    @debug.command(name='unloadappcmd')
    async def unload_app_cmd(self, ctx: Context, cmd: str, resync: bool | None = False):
        """Unload an application command."""
        unloaded = self.client.tree.remove_command(cmd)

        if resync:
            try:
                await self.client.tree.sync()
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
            guild = GUILD_OBJECT
            target_fmt = 'current guild'

        try:
            synced = await self.client.tree.sync(guild=guild)
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
            guild = GUILD_OBJECT
            target_fmt = 'current guild'

        cmds = await self.client.tree.fetch_commands(guild=guild)

        fmt = '\n'.join(repr(cmd) for cmd in cmds) if cmds else 'None'
        await ctx.reply(
            f':white_check_mark: Fetched {len(cmds)} command(s) in **{target_fmt}**:\n`{fmt}`'
        )

    @debug.command()
    async def clear(self, ctx: Context):
        os.system('cls||clear')  # noqa: ASYNC221, S605, S607
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
        guilds = len(self.client.guilds)
        await self.client.change_presence(
            activity=discord.Game(f'with {guilds} moisturized servers')
        )
        await ctx.send(':white_check_mark: Status updated.')

    @commands.is_owner()
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
            emoji = await self.client.http.get_from_cdn(emoji_link)
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
        """Delete a Sticker from the guild"""

        sticker = discord.utils.get(ctx.guild.stickers, name=alias)
        if sticker is None:
            await ctx.reply(':no_entry: Sticker not found.')
            return

        await ctx.guild.delete_sticker(sticker)
        await ctx.reply(':white_check_mark: Deleted sticker.')

    @commands.command(hidden=True, name='eval')
    async def _eval(self, ctx: Context, *, body: str):
        """Evaluates python code."""

        # I'm sorry but this was way too cool not to yoink :3
        # https://github.com/Rapptz/RoboDanny/blob/a52a212d1fff1024fb00c14b9e125071f87e0323/cogs/admin.py#L215C31-L215C31

        env = {
            'ctx': ctx,
            'self': self,
            'guild': ctx.guild,
            'author': ctx.author,
            'client': self.client,
            'channel': ctx.channel,
            'message': ctx.message,
            '_': self._last_result,
        }

        env.update(globals())

        body = self.cleanup_code(body)
        stdout = io.StringIO()

        to_compile = f'async def func():\n{textwrap.indent(body, "  ")}'

        try:
            exec(to_compile, env)
        except Exception as e:  # noqa: BLE001
            return await ctx.send(f'```py\n{e.__class__.__name__}: {e}\n```')

        func = env['func']
        try:
            with redirect_stdout(stdout):
                ret = await func()  # type: ignore[]
        except Exception:  # noqa: BLE001
            value = stdout.getvalue()
            await ctx.send(f'```py\n{value}{traceback.format_exc()}\n```')
        else:
            value = stdout.getvalue()
            try:
                await ctx.message.add_reaction('\u2705')
            except Exception:  # noqa: BLE001, S110
                pass

            if ret is None:
                if value:
                    await ctx.send(f'```py\n{value}\n```')
            else:
                self._last_result = ret
                await ctx.send(f'```py\n{value}{ret}\n```')

    @commands.command(hidden=True)
    async def repl(self, ctx: Context):
        """Launches an interactive REPL session."""

        # This is so cool I couldn't resist qwq
        # https://github.com/Rapptz/RoboDanny/blob/a52a212d1fff1024fb00c14b9e125071f87e0323/cogs/admin.py#L262

        variables = {
            'ctx': ctx,
            'self': self,
            'guild': ctx.guild,
            'author': ctx.author,
            'client': self.client,
            'message': ctx.message,
            'channel': ctx.channel,
            '_': None,
        }

        if ctx.channel.id in self.sessions:
            await ctx.send(
                'Already running a REPL session in this channel. Exit it with `quit`.'
            )
            return

        self.sessions.add(ctx.channel.id)
        await ctx.send('Enter code to execute or evaluate. `exit()` or `quit` to exit.')

        def check(m: discord.Message):
            return (
                m.author.id == ctx.author.id
                and m.channel.id == ctx.channel.id
                and m.content.startswith('`')
            )

        while True:
            try:
                response = await self.client.wait_for(
                    'message', check=check, timeout=10.0 * 60.0
                )
            except TimeoutError:
                await ctx.send('Exiting REPL session.')
                self.sessions.remove(ctx.channel.id)
                break

            cleaned = self.cleanup_code(response.content)

            if cleaned in {'quit', 'exit', 'exit()'}:
                await ctx.send('Exiting.')
                self.sessions.remove(ctx.channel.id)
                return

            executor = exec
            code = ''
            if cleaned.count('\n') == 0:
                # Single statement, potentially 'eval'
                try:
                    code = compile(cleaned, '<repl session>', 'eval')
                except SyntaxError:
                    pass
                else:
                    executor = eval

            if executor is exec:
                try:
                    code = compile(cleaned, '<repl session>', 'exec')
                except SyntaxError as e:
                    await ctx.send(self.get_syntax_error(e))
                    continue

            variables['message'] = response

            fmt = None
            stdout = io.StringIO()

            try:
                with redirect_stdout(stdout):
                    result = executor(code, variables)
                    if inspect.isawaitable(result):
                        result = await result
            except Exception:  # noqa: BLE001
                value = stdout.getvalue()
                fmt = f'```py\n{value}{traceback.format_exc()}\n```'
            else:
                value = stdout.getvalue()
                if result is not None:
                    fmt = f'```py\n{value}{result}\n```'
                    variables['_'] = result
                elif value:
                    fmt = f'```py\n{value}\n```'

            try:
                if fmt is not None:
                    if len(fmt) > 2000:
                        await ctx.send('Content too big to be printed.')
                    else:
                        await ctx.send(fmt)
            except discord.Forbidden:
                pass
            except discord.HTTPException as e:
                await ctx.send(f'Unexpected error: `{e}`')

    @commands.command(name='health', aliases=['stats'])
    @commands.is_owner()
    async def _bot_stats(self, ctx: Context):
        """Various bot stat monitoring tools."""

        HEALTHY = discord.Color(value=0x43B581)  # noqa: N806
        UNHEALTHY = discord.Color(value=0xF04947)  # noqa: N806
        # WARNING = discord.Color(value=0xF09E47)

        # Process stats
        process = self.process
        with process.oneshot():
            cpu_usage = process.cpu_percent() / (psutil.cpu_count() or 1)
            thread_count = process.num_threads()
            memory = process.memory_full_info()
            name = process.name()
            pid = process.pid

            physical_memory = memory.rss / 1024**2
            virtual_memory = memory.vms / 1024**2
            unique_memory = memory.uss / 1024**2

        # Message cache stats
        if self.client._connection.max_messages:  # noqa: SLF001
            message_cache = f'{len(self.client.cached_messages)}/{self.client._connection.max_messages}'  # noqa: SLF001
        else:
            message_cache = 'Disabled'

        # Tasks stats
        all_tasks = asyncio.all_tasks(loop=self.client.loop)
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

        python_version, _, _ = sys.version.partition('(')

        embed = (
            discord.Embed(
                title='Bot Stats Report',
                color=HEALTHY,
                timestamp=discord.utils.utcnow(),
            )
            .add_field(
                name='Process',
                value=f'{cpu_usage:.2f}% CPU\n'
                f'Threads: {thread_count}\n'
                f'Name: "{name}"\n'
                f'PID: {pid}',
                inline=True,
            )
            .add_field(
                name='Memory',
                value=f'Physical: {physical_memory:.2f} MiB\n'
                f'Unique: {unique_memory:.2f} MiB\n'
                f'Virtual: {virtual_memory:.2f} MiB',
                inline=True,
            )
            .add_field(
                name='Cache',
                value=f'Guilds: {len(self.client.guilds)}\n'
                f'Users: {len(self.client.users)}\n'
                f'Messages: {message_cache}',
                inline=True,
            )
            .add_field(
                name='Events Waiting',
                value=f'Total: {len(event_tasks)}\nFuture task: {len(future_tasks)}',
                inline=False,
            )
            .add_field(
                name='Distribution',
                value=f'{dist_version}\n'
                f'Jishaku: v{package_version("jishaku")}\n'
                f'Python: v{python_version}\n'
                f'Platform: {sys.platform}',
                inline=False,
            )
        )

        description: list[str] = []

        started_at = discord.utils.format_dt(self.client.started_at, 'R')
        description.append(f'Started: {started_at}')

        global_rate_limit = not self.client.http._global_over.is_set()  # noqa: SLF001
        description.append(f'Global Rate Limit: {global_rate_limit}')

        if global_rate_limit:
            embed.color = UNHEALTHY

        embed.description = '\n'.join(description)
        await ctx.reply(embed=embed)


async def setup(client: MoistBot) -> None:
    await client.add_cog(OwnerOnly(client))

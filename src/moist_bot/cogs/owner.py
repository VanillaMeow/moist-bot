# ruff: noqa: F401, S102, S307
# pyright: reportUnusedImport=false, reportPrivateUsage=false

from __future__ import annotations

import asyncio
import datetime
import gc
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
from typing import TYPE_CHECKING, cast

import discord
import discord.utils
from discord.ext import commands
from sqlalchemy import delete
from sqlmodel import select

from moist_bot.models import RESTART_NOTICE_ID, RestartNotice
from moist_bot.utils.converters import normalize_datetime

if TYPE_CHECKING:
    from typing import Any

    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


log = logging.getLogger('discord.' + __name__)


class Owner(commands.Cog):
    """Debug commands that only the bot owner can use."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

        # Cog
        self._last_result: Any = None
        self.sessions: set[int] = set()
        self._handled_restart_notice: bool = False

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{HAMMER AND WRENCH}')

    async def cog_check(self, ctx: Context) -> bool:  # type: ignore[reportIncompatibleMethodOverride]
        if not await ctx.bot.is_owner(ctx.author):
            raise commands.NotOwner('You do not own this bot.')
        return True

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Edit the restart notice once the bot is available again."""

        if self._handled_restart_notice:
            return

        self._handled_restart_notice = True
        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(RestartNotice).where(RestartNotice.id == RESTART_NOTICE_ID)
            )
            notice = result.scalar_one_or_none()
            if notice is None:
                return

            await session.delete(notice)
            await session.commit()

        try:
            message = await self.get_restart_notice_message(notice)
            requested_at = normalize_datetime(notice.requested_at)
            elapsed = max((discord.utils.utcnow() - requested_at).total_seconds(), 0.0)
            await message.edit(
                content=f':white_check_mark: Restarted in {elapsed:.2f}s.'
            )
        except discord.DiscordException:
            log.exception(
                f'Failed to update restart notice message {notice.message_id} in channel {notice.channel_id}.'
            )

    async def get_restart_notice_message(
        self,
        notice: RestartNotice,
    ) -> discord.Message:
        """Return the Discord message for a stored restart notice."""

        if notice.guild_id is None:
            channel = await self.bot.get_or_fetch_channel(notice.channel_id)
        else:
            guild = await self.bot.get_or_fetch_guild(notice.guild_id)
            channel = await self.bot.get_or_fetch_channel(
                notice.channel_id,
                guild=guild,
            )

        messageable = cast('discord.abc.Messageable', channel)
        return await self.bot.get_or_fetch_message(notice.message_id, messageable)

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

    @commands.command(hidden=True)
    async def restart(self, ctx: Context) -> None:
        """Restart the bot process."""

        message = await ctx.reply(':arrows_counterclockwise: Restarting...')
        guild_id = ctx.guild.id if ctx.guild is not None else None
        async with self.bot.db_session_maker() as session:
            await session.execute(delete(RestartNotice))
            session.add(
                RestartNotice(
                    guild_id=guild_id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    requested_by_id=ctx.author.id,
                )
            )
            await session.commit()

        log.warning(f'Restart requested by {ctx.author} ({ctx.author.id}).')
        argv = [sys.executable, *sys.argv]
        try:
            await self.bot.close()
        except asyncio.CancelledError:
            pass
        finally:
            # Closing the bot can cancel the command task before restart
            os.execv(sys.executable, argv)  # noqa: S606

    @commands.command(hidden=True, name='eval')
    async def _eval(self, ctx: Context, *, body: str):
        """Evaluates python code."""

        # I'm sorry but this was way too cool not to yoink :3
        # https://github.com/Rapptz/RoboDanny/blob/a52a212d1fff1024fb00c14b9e125071f87e0323/cogs/admin.py#L215C31-L215C31

        env = {
            'ctx': ctx,
            'self': self,
            'bot': self.bot,
            'guild': ctx.guild,
            'author': ctx.author,
            'client': self.bot,
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
            'bot': self.bot,
            'guild': ctx.guild,
            'client': self.bot,
            'author': ctx.author,
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
                response = await self.bot.wait_for(
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

    @commands.command(name='quit', hidden=True)
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


async def setup(client: MoistBot) -> None:
    await client.add_cog(Owner(client))

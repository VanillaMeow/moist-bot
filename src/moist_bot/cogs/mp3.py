# pyright: reportMissingTypeStubs=false, reportIncompatibleMethodOverride=false

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord
import pytube.exceptions
from discord.ext import commands
from pytube import YouTube

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


log = logging.getLogger('discord.' + __name__)


class FileTooBig(commands.CommandError):
    pass


class Mp3(commands.Cog):
    def __init__(self, client: MoistBot):
        self.client: MoistBot = client
        self.execute = self.client.loop.run_in_executor

    @staticmethod
    def _get_buffer(url: str) -> tuple[io.BytesIO, str]:
        # Get audio stream
        audio_stream = YouTube(url).streams.get_audio_only()

        if audio_stream is None:
            raise pytube.exceptions.PytubeError from commands.BadArgument

        # Check file size
        if audio_stream.filesize_approx > 8_388_608:
            raise FileTooBig

        # Download
        buffer = io.BytesIO()
        audio_stream.stream_to_buffer(buffer)
        buffer.seek(0)
        return buffer, audio_stream.title

    @commands.command(hidden=True)
    @commands.cooldown(rate=1, per=10, type=commands.BucketType.user)
    @commands.is_owner()
    async def mp3(self, ctx: Context, *, url: str):
        """Youtube mp3 downloader."""

        async with ctx.typing():
            # Gets buffer in separate thread to avoid blocking
            buffer, title = await self.execute(None, self._get_buffer, url)
            await ctx.reply(file=discord.File(fp=buffer, filename=f'{title}.mp3'))
            buffer.close()

    async def cog_command_error(self, ctx: Context, error: Exception) -> None:
        error = getattr(error, 'original', error)

        # File too big
        if isinstance(error, FileTooBig):
            await ctx.reply(':warning: File is over 8MiB and cannot be sent.')

        # Pytube regex error
        elif isinstance(error, pytube.exceptions.RegexMatchError):
            await ctx.reply(f':warning: Cannot find video url `{ctx.kwargs["url"]}`.')

        # Pytube base error
        elif isinstance(error, pytube.exceptions.PytubeError):
            log.error(type(error), exc_info=error)
            await ctx.reply(':warning: Cannot download the audio.')

        # This shouldn't happen
        elif isinstance(error, discord.HTTPException):
            try:
                await ctx.reply(':warning: HTTP Error.')
                log.error(type(error), exc_info=error)
            except discord.DiscordException:
                # probably missing perms
                return


async def setup(client: MoistBot) -> None:
    await client.add_cog(Mp3(client))

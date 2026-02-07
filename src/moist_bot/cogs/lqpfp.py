from __future__ import annotations

import io
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from PIL import Image

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


class AvatarEmbed(discord.Embed):
    def __init__(
        self,
        user: discord.User,
    ):
        super().__init__(
            type='image',
            color=user.accent_color or discord.Color.random(),
        )
        self.set_image(url='attachment://image.png')
        self.set_author(
            name=f"{user.display_name}'s low quality avatar",
            icon_url='attachment://image.png',
        )


class LowQualityProfilePicture(commands.Cog):
    def __init__(self, client: MoistBot):
        self.client: MoistBot = client
        self.executor = ProcessPoolExecutor()
        self.execute = self.client.loop.run_in_executor

    async def cog_unload(self) -> None:
        self.executor.shutdown(wait=False)

    @staticmethod
    def _get_buffer(img_bytes: bytes, lq_f: float = 1) -> io.BytesIO:
        img = Image.open(io.BytesIO(img_bytes))

        s, _ = org_size = img.size
        lq_size = round((15 / 100 * s) / lq_f) or 1

        img = img.resize((lq_size, lq_size), Image.Resampling.NEAREST)
        img = img.resize(org_size, Image.Resampling.NEAREST)

        buffer = io.BytesIO()
        img.save(buffer, 'png')
        buffer.seek(0)
        return buffer

    @commands.cooldown(rate=1, per=2, type=commands.BucketType.user)
    @commands.command()
    async def lqpfp(
        self, ctx: Context, user: discord.User = commands.Author, factor: float = 1
    ):
        """Display a low quality version of a user's avatar."""

        # Limit low quality factor
        if factor < 1 or factor > 999999999:
            raise commands.BadArgument('Size must be between 1 and 999999999')

        async with ctx.typing():
            # Get avatar
            avatar: bytes = (
                await user.display_avatar.with_format('png').with_size(2048).read()
            )

            # Avoid blocking
            img_buffer = await self.execute(
                self.executor, self._get_buffer, avatar, factor
            )

            # Send image
            file = discord.File(fp=img_buffer, filename='image.png')
            await ctx.reply(file=file, embed=AvatarEmbed(user))


async def setup(client: MoistBot) -> None:
    await client.add_cog(LowQualityProfilePicture(client))

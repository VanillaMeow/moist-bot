from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated

from discord.ext import commands
from discord.utils import escape_mentions

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


class Cmds(commands.Cog):
    def __init__(self, client: MoistBot):
        self.client: MoistBot = client

    @commands.command()
    @commands.cooldown(rate=1, per=3, type=commands.BucketType.user)
    async def ping_ws(self, ctx: Context):
        """Discord websocket protocol latency."""
        await ctx.reply(
            f'Discord websocket latency is {round(self.client.latency * 1000)}ms'
        )

    @commands.command()
    @commands.cooldown(rate=1, per=3, type=commands.BucketType.user)
    async def ping(
        self, ctx: Context, *, msg: Annotated[str, escape_mentions] = 'Pong!'
    ):
        """Measures a single round trip time."""
        before = time.monotonic()
        message = await ctx.reply(f'{msg}')
        ping = (time.monotonic() - before) * 1000
        await message.edit(content=f'{msg} in {int(ping)}ms')

    @commands.command()
    async def say(self, ctx: Context, *, msg: Annotated[str, escape_mentions]):
        await ctx.send(msg)

    @commands.command()
    async def stutter(self, ctx: Context, *, msg: Annotated[str, escape_mentions]):
        stuttered_msg = '  '.join(
            f'{word[0]}-{word[0]}-{word}' for word in msg.split(' ')
        )

        if len(stuttered_msg) >= 2000:
            await ctx.reply(':warning: Message over character limit.')
            return

        await ctx.reply(stuttered_msg)

    # Event listeners
    # @commands.Cog.listener()
    # async def on_message(self, message: discord.Message):
    #
    #     # Don't reply to self
    #     if message.author == self.client.user:
    #         return
    #
    #     if message.content.lower() == 'water':
    #         await message.channel.send('water')


async def setup(client: MoistBot) -> None:
    await client.add_cog(Cmds(client))

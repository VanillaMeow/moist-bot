from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


class CooldownTest(commands.Cog):
    def __init__(self, client: MoistBot):
        self.client: MoistBot = client

    @commands.is_owner()
    @commands.cooldown(rate=1, per=10, type=commands.BucketType.user)
    @commands.command(hidden=True)
    async def cdt(self, ctx: Context, user: discord.Member = commands.Author):
        after = discord.utils.utcnow() + datetime.timedelta(seconds=10)
        dt = discord.utils.format_dt(after, style='R')

        await ctx.reply(f'{user.display_name}: {dt}', delete_after=10)
        await ctx.message.add_reaction('✅')


async def setup(client: MoistBot) -> None:
    await client.add_cog(CooldownTest(client))

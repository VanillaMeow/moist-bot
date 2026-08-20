from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


log = logging.getLogger('discord.' + __name__)

FLEASION_GUILD_ID = 1309760132770693181
FLEASION_CHANNEL_IDS = frozenset(
    (
        1309904275932975214,  # moderation-logs
    )
)


class Fleasion(commands.Cog):
    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{CRICKET}')

    def cog_check(self, ctx: Context) -> bool:  # type: ignore[]
        return bool(ctx.guild) and ctx.guild.id == FLEASION_GUILD_ID

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        bot_user = self.bot.user or 'Fleabot'
        if message.channel.id not in FLEASION_CHANNEL_IDS:
            return

        if any(
            embed.description is not None and str(bot_user) in embed.description
            for embed in message.embeds
        ):
            await message.delete()


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(Fleasion(bot))

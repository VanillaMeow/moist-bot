from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context

    class GuildMessage(discord.Message):
        """A message in a guild."""

        guild: discord.Guild
        author: discord.Member  # type: ignore[reportIncompatibleVariableOverride]


log = logging.getLogger('discord.' + __name__)


FLEASION_GUILD_ID = 1309760132770693181
FLEASION_CLEANUP_CHANNEL_IDS = frozenset(
    (
        1309904275932975214,  # moderation-logs
    )
)

HELP_KEYWORDS = {'help', 'how to'}
FLEASION_HELP_CHANNEL_IDS = frozenset(
    (
        1495010741940654182,  # general
    )
)


class Fleasion(commands.Cog):
    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

        # Temporary until I figure out if this is viable
        self.testing_channel = cast(
            'discord.TextChannel', bot.get_channel(1548068976104579142)
        )

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{CRICKET}')

    def cog_check(self, ctx: Context) -> bool:  # type: ignore[]
        return bool(ctx.guild) and ctx.guild.id == FLEASION_GUILD_ID

    @commands.Cog.listener(name='on_message')
    async def on_cleanup_message(self, message: discord.Message):  # pyright: ignore[reportRedeclaration]
        bot_user = self.bot.user or 'Fleabot'
        if message.channel.id not in FLEASION_CLEANUP_CHANNEL_IDS:
            return

        if any(
            embed.description is not None and str(bot_user) in embed.description
            for embed in message.embeds
        ):
            await message.delete()

    @commands.Cog.listener(name='on_message')
    async def on_help_message(self, message: discord.Message):
        if message.channel.id not in FLEASION_HELP_CHANNEL_IDS:
            return
        message = cast('GuildMessage', message)

        # Main criteria
        contents = message.content.lower()
        if not any(keyword in contents for keyword in HELP_KEYWORDS):
            return

        # We want to catch only new members
        for role in message.author.roles:
            if '[' in role.name:  # Level role (e.g. "Meow [L1]")
                break
        else:
            return

        # TODO(leah): Figure out if this is viable
        # For now just forward to the testing channel
        await message.forward(self.testing_channel)


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(Fleasion(bot))

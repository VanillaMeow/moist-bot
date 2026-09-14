from __future__ import annotations

import logging
from functools import cached_property
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands

from moist_bot.settings import settings

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


HELP_KEYWORDS = {'help', 'how to', 'how get', 'how do', 'what do', 'why are'}
FLEASION_HELP_CHANNEL_ID = 1495014874831655052
FLEASION_HELP_CHANNEL_IDS = frozenset(
    (
        1495010741940654182,  # general
        1548397150520737802  # fleabot-help-testing-input
        if settings.is_fleabot
        else 1548748199857225771,  # moist-help-testing-input
    )
)
HELP_TEST_CHANNEL = 1548068976104579142 if settings.is_fleabot else 1548748231242940436


class Fleasion(commands.Cog):
    test_channel: discord.TextChannel

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

        help_skip_count = 2
        self.help_cooldown = commands.CooldownMapping['GuildMessage'].from_cooldown(
            rate=help_skip_count + 1,
            per=60 * 2,
            type=commands.BucketType.user,
        )

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{CRICKET}')

    @cached_property
    def help_message(self) -> str:
        help_channel = self.bot.get_partial_messageable(
            FLEASION_HELP_CHANNEL_ID,
            guild_id=FLEASION_GUILD_ID,
            type=discord.ChannelType.text,
        )
        return f'Use {help_channel.mention}. **Please do not ask for help here.**'

    def cog_check(self, ctx: Context) -> bool:  # type: ignore[]
        return bool(ctx.guild) and ctx.guild.id == FLEASION_GUILD_ID

    @commands.Cog.listener()
    async def on_ready(self):
        """Populate the testing channel field after the cache is ready."""
        test_channel = self.bot.get_channel(HELP_TEST_CHANNEL)
        if test_channel is None:
            log.warning('Testing channel not found.')
            return
        self.test_channel = cast('discord.TextChannel', test_channel)

    @commands.Cog.listener(name='on_message')
    async def on_cleanup_message(self, message: discord.Message):
        """Delete messages that mention Fleabot in specific channel embeds."""
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
        """Handle various automated Fleasion help messages."""
        if message.channel.id not in FLEASION_HELP_CHANNEL_IDS:
            return
        message = cast('GuildMessage', message)

        # Global exceptions
        if message.author.bot or message.webhook_id is not None:
            return

        if await self._handle_help_message(message):
            return

    async def _handle_help_message(self, message: GuildMessage) -> bool:
        """Handle telling users to use the help channel instead.

        Returns
        -------
        bool
            Whether the message was handled.
        """
        # Main criteria
        contents = message.content.lower()
        if not any(keyword in contents for keyword in HELP_KEYWORDS):
            return False

        # We want to catch only new members
        for role in message.author.roles:
            if '[' in role.name:  # Level role (e.g. "Meow [L1]")
                return False

        if self._is_on_cooldown(self.help_cooldown, message):
            return True

        # Notify user and log the message
        await message.reply(self.help_message)
        await message.forward(self.test_channel)
        return True

    def _is_on_cooldown(
        self, cooldown: commands.CooldownMapping[GuildMessage], message: GuildMessage
    ) -> bool:
        """Return and update whether the cooldown is active.

        Returns
        -------
        bool
            Whether the cooldown is active.
        """
        current = message.created_at.timestamp()
        bucket = cooldown.get_bucket(message, current)
        if bucket is None:  # Shouldn't happen
            return True

        # Handle cooldowns
        tokens = bucket.get_tokens(current)
        if 0 < tokens < bucket.rate:
            # Skip the next `rate - 1` triggers within the current window
            bucket.update_rate_limit(current)
            return True

        # Log the first trigger, or restart after skips or expiry
        bucket.reset()
        bucket.update_rate_limit(current)
        return False


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(Fleasion(bot))

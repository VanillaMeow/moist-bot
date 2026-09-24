from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands

from moist_bot.services import fleasion, tracking
from moist_bot.services.fleasion import (
    HelpMessageEnum,
    JevHelpClassifier,
    RegexHelpClassifier,
)
from moist_bot.settings import settings
from moist_bot.utils.reload import deep_reload

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.services.fleasion import JevHelpModel
    from moist_bot.types import GuildMessage
    from moist_bot.utils.context import Context


log = logging.getLogger('discord.' + __name__)


# Help and activity tracking
FLEASION_HELP_CHANNEL_ID = 1495014874831655052  # help-chat
FLEASION_CONFIG_CHANNEL_ID = 1463234573797167358  # configs
FLEASION_DOWNLOAD_CHANNEL_ID = 1466669492284166209  # download
FLEASION_HELP_CHANNEL_IDS = frozenset(
    (
        1495010741940654182,  # general
        1548397150520737802  # fleabot-help-testing-input
        if settings.is_fleabot
        else 1548748199857225771,  # moist-help-testing-input
    )
)
HELP_TEST_CHANNEL = 1549081891225866310 if settings.is_fleabot else 1548748231242940436


# Channel cleanup
FLEASION_GUILD_ID = 1309760132770693181
FLEASION_CLEANUP_CHANNEL_IDS = frozenset(
    (
        1309904275932975214,  # moderation-logs
    )
)


class Fleasion(commands.Cog):
    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

        namespace = 'fleabot:fleasion' if settings.is_fleabot else 'moistbot:fleasion'
        config = tracking.TrackingConfig(
            activity_window=120 if settings.is_fleabot else 1,
            activity_threshold=2,
            help_window=120 if settings.is_fleabot else 1,
            help_skip_count=2,
            repost_window=600,
            repost_similarity=0.9,
            repost_min_length=20,
            warning_window=120,
        )
        self.tracking = tracking.TrackingService(bot, namespace, config=config)
        self.is_testing: bool = False

        # Jev Experimental
        self._jev_classifier = JevHelpClassifier(
            settings.jev_token.get_secret_value(), settings.jev_model
        )

        # Partials
        self.help_channel = self.bot.get_partial_messageable(
            FLEASION_HELP_CHANNEL_ID,
            guild_id=FLEASION_GUILD_ID,
            type=discord.ChannelType.text,
        )
        self.config_channel = self.bot.get_partial_messageable(
            FLEASION_CONFIG_CHANNEL_ID,
            guild_id=FLEASION_GUILD_ID,
            type=discord.ChannelType.forum,
        )
        self.download_channel = self.bot.get_partial_messageable(
            FLEASION_DOWNLOAD_CHANNEL_ID,
            guild_id=FLEASION_GUILD_ID,
            type=discord.ChannelType.text,
        )
        self.test_channel = self.bot.get_partial_messageable(
            HELP_TEST_CHANNEL,
            guild_id=FLEASION_GUILD_ID,
            type=discord.ChannelType.text,
        )

        # Responses
        self.HELP_MESSAGE = (
            f'Use {self.help_channel.mention}. **Please do not ask for help here.**'
        )
        self.CONFIG_MESSAGE = f'Check {self.config_channel.mention} for configs.'
        self.DOWNLOAD_MESSAGE = (
            f'Download Fleasion from {self.download_channel.mention}.'
        )
        self.REPOST_MESSAGE_PARTIAL = (
            'Please do not repost your help message here. '
            f'Continue in {self.help_channel.mention} '
            'and wait for a reply to [your original message]({}).'
        )

        # Bindings
        self.HELP_MESSAGE_BINDINGS = {
            HelpMessageEnum.CONFIG: self.CONFIG_MESSAGE,
            HelpMessageEnum.DOWNLOAD: self.DOWNLOAD_MESSAGE,
            HelpMessageEnum.HELP: self.HELP_MESSAGE,
        }

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{CRICKET}')

    async def cog_load(self) -> None:
        # Restore unexpired state before the cog starts receiving messages
        try:
            await self.tracking.load()
        except Exception:
            await self._jev_classifier.close()
            raise

    async def cog_unload(self) -> None:
        await self._jev_classifier.close()

    def cog_check(self, ctx: Context) -> bool:  # type: ignore[]
        return bool(ctx.guild) and ctx.guild.id == FLEASION_GUILD_ID

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
        if message.guild is None:
            return
        message = cast('GuildMessage', message)

        is_help_channel = message.channel.id == FLEASION_HELP_CHANNEL_ID
        if not is_help_channel and message.channel.id not in FLEASION_HELP_CHANNEL_IDS:
            return

        # Global exceptions
        if (
            message.author.bot
            or message.webhook_id is not None
            or self._member_has_level_role(message.author)
        ):
            return

        if is_help_channel:
            await self.tracking.record_repost_source(message)
            return

        if await self._handle_help_repost(message):
            return

        if await self._handle_help_message(message):
            return

        await self.tracking.record_activity(message)

    async def _handle_help_repost(self, message: GuildMessage) -> bool:
        """Warn about cross-posts before ordinary help and conversation checks.

        Returns
        -------
        bool
            Whether the message was handled.
        """
        original_id = await self.tracking.find_repost(message)
        if original_id is None:
            return False
        if not await self.tracking.claim_repost_warning(message):
            return True

        original_message = self.help_channel.get_partial_message(original_id)
        reply = self.REPOST_MESSAGE_PARTIAL.format(original_message.jump_url)
        await self._send_reply(reply, message)
        return True

    async def _handle_help_message(self, message: GuildMessage) -> bool:
        """Direct users to the help or config channel for their request.

        Returns
        -------
        bool
            Whether the message was handled.
        """
        # Main criteria
        help_type = RegexHelpClassifier.classify(message.content)
        self._jev_classifier.classify_background(
            message.content,
            on_result=partial(self._send_jev_preview, message, help_type),
            name=f'jev-preview:{message.id}',
        )

        if help_type is HelpMessageEnum.NONE:
            return False

        if await self.tracking.check_help_cooldown(message):
            return True

        if await self.tracking.is_active(message):
            return True

        reply = self.HELP_MESSAGE_BINDINGS.get(help_type, self.HELP_MESSAGE)
        await self._send_reply(reply, message)
        return True

    async def _send_jev_preview(
        self, message: GuildMessage, regex_type: HelpMessageEnum, result: JevHelpModel
    ) -> None:
        if result.help_type is HelpMessageEnum.NONE:
            return

        # TODO(leah): Add when production is ready
        # if result.confidence < 0.9:
        #     return

        jev_label = result.help_type.name
        regex_label = regex_type.name
        reply = (
            f'**Jev**: {jev_label} (confidence: {result.confidence:.0%}) '
            f'| **Regex**: {regex_label}'
        )
        await self._send_reply(reply, message, force_test=True)

    @staticmethod
    def _member_has_level_role(member: discord.Member) -> bool:
        """Return whether the user has a level role."""
        # Level role (e.g. "Meow [L1]")
        return any('[' in role.name for role in member.roles)

    async def _send_reply(
        self, reply: str, message: discord.Message, *, force_test: bool = False
    ) -> None:
        """Send a test preview with the original message, or reply to the user."""
        if self.is_testing or force_test:
            await self.test_channel.send(reply)
            await message.forward(self.test_channel)
            return

        await message.reply(reply)

    @commands.command()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(rate=1, per=5, type=commands.BucketType.guild)
    async def toggle(self, ctx: Context) -> None:
        """Toggle this server's fleabot help channel."""
        self.is_testing = not self.is_testing
        await ctx.reply(f'Test mode is now {self.is_testing!s}.')


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(Fleasion(bot))


async def teardown(_bot: MoistBot) -> None:
    try:
        deep_reload(tracking)
        deep_reload(fleasion)
    except Exception:
        log.exception('Failed to reload tracking or Fleasion services during teardown')
        raise

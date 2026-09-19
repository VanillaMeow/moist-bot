from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import discord
from discord.ext import commands

from moist_bot.services.tracking import TrackingConfig, TrackingService
from moist_bot.settings import settings

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.types import GuildMessage
    from moist_bot.utils.context import Context


HELP_SOMEONE = r'(?:someone|somone|somebody|anyone|anybody|some\s*1|any\s*1)'
HELP_GREETING = (
    r'(?:(?:yo+|hey|hi|hello|hiya|sup|ayo|ey|oi|guys|chat|'
    r'y[\x27\u2019]?all|everyone|anybody|folks|people|gang|team|'
    r'bro|bros|bruh|bruv|boi|dude|man|mate|pls|plz|please|so|also)\W+)*'
)
HELP_APP_NAME = r'fleasi?on\b'
HELP_APP_ACTION = r'(?:work(?:s|ing)?|run(?:s|ning)?|open(?:s|ing)?|launch(?:es|ing)?|load(?:s|ing)?)\b'
HELP_APP_STATUS = (
    r'(?:\s+on\s+\w+)?\s+(?:(?:still|not|even|actually|currently|just)\s+)*'
    rf'(?:{HELP_APP_ACTION}|crash(?:es|ing)?\b|broken\b|down\b|offline\b|online\b)'
)
HELP_REQUEST_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Ask about the app's status without requiring a particular operating system
        (
            rf'(?:^|[.!?]\s+){HELP_GREETING}(?:why\s+)?'
            r'(?:is|isn[\x27\u2019]?t|does|doesn[\x27\u2019]?t|did|has|will|'
            r'won[\x27\u2019]?t|can|can[\x27\u2019]?t)\s+(?:my\s+)?'
            rf'{HELP_APP_NAME}{HELP_APP_STATUS}'
        ),
        (
            rf'\b{HELP_SOMEONE}\s+know\s+(?:if|why|whether)\s+'
            rf'(?:is\s+|does\s+)?{HELP_APP_NAME}'
            rf'(?:\s+is|\s+does)?{HELP_APP_STATUS}'
        ),
        # Failure reports often omit question words and punctuation entirely
        (
            rf'(?:^|[.!?]\s+){HELP_GREETING}(?:why\s+)?(?:my\s+)?{HELP_APP_NAME}'
            r'\s+(?:(?:(?:is\s+)?(?:still\s+)?not|isn[\x27\u2019]?t|'
            r'doesn[\x27\u2019]?t|won[\x27\u2019]?t|stopped)'
            rf'\s+(?:(?:even|actually)\s+)?{HELP_APP_ACTION}|'
            r'(?:keeps|is)\s+crashing\b)'
        ),
        # Require questions to start a message or sentence, after optional greetings
        (
            rf'(?:^|[.!?]\s+){HELP_GREETING}'
            r'(?:hel+p+\b|(?:need|want)\s+(?:(?:some|sum)\s+)?hel+p+\b|'
            r'how\s+(?:to|get|do|can|should)\b|what\s+(?:do|should|can)\b|'
            r'what\s+does\s+(?:this|that|it)\s+mean\b|'
            r'why\s+(?:is|are|does|do)\s+(?:my|the|this|that|it)\b)'
        ),
        # Keep explicit requests separate from offers, thanks and mentions of help
        (
            r'\b(?:i|we)\s+(?:(?:just|js|really|rlly)\s+)*'
            r'(?:need|want)\s+(?:(?:some|sum)\s+)?hel+p+\b'
        ),
        (
            rf'\b{HELP_SOMEONE}\s+'
            r'(?:(?:here|can|could|just|js|pls|plz|please)\s+)*hel+p+\b'
        ),
        r'\b(?:pls|plz|please)\s+hel+p+\b',
        r'\bhel+p+\s+me+\b',
        r'\bwho\s+(?:can|could)\s+hel+p+\b',
        r'\b(?:can|could)\s+i\s+(?:get|have)\s+(?:(?:some|sum)\s+)?hel+p+\b',
        r'\bany\s+help\s+(?:would|will)\s+be\s+appreciated\b',
        # Requests can include a call or other context before the actual help verb
        (
            rf'\b(?:can|could|would|will)\s+(?:{HELP_SOMEONE}|you|u)\b'
            r'[^.!?]{0,100}\b(?:hel+p+|show|teach|explain)\b'
        ),
        (
            rf'\b(?:can|could|would|will)\s+(?:{HELP_SOMEONE}|you|u)\b'
            r'[^.!?]{0,40}\b(?:tell|dm)\s+me\b[^.!?]{0,40}\bhow\s+to\b'
        ),
        # Indirect questions and admissions of uncertainty still ask for instructions
        (
            rf'\b{HELP_SOMEONE}\s+(?:of\s+you\s+guys\s+)?'
            r'know\s+how\s+to\b'
        ),
        r'\bdo\s+(?:(?:you|u)\s+know|yk)\s+how\s+to\b',
        r'\b(?:idk|i\s+don[\x27\u2019]?t\s+know)\b[^.!?]{0,60}\bhow\s+to\b',
        (
            r'\bis\s+there\s+(?:a|an)\s+(?:vid|video|tutorial|tuto)\b'
            r'[^.!?]{0,60}\bhow\s+to\b'
        ),
        r'\b(?:can|could|may)\s+i\s+ask\s+for\s+help\b',
        r'\bis\s+it\s+(?:ok|okay)\s+if\s+i\s+ask\s+for\s+help\b',
    )
)


CONFIG_NAME = r'\b(?:cnfg|cfg|config)s?\b'
CONFIG_REQUEST_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Ask whether someone has a config, including common chat abbreviations
        (
            rf'\b(?:{HELP_SOMEONE}|anb|you|u)\s+(?:have|has|got|know)\s+'
            rf'(?!why\b|how\b|if\b|whether\b)[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
        # Ask another user to share, send or create a config
        (
            rf'\b(?:can|could|would|will)\s+(?:{HELP_SOMEONE}|anb|you|u)\s+'
            rf'(?:share|send|give|link|make)\b[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
        rf'\b(?:send|give|link|make)\s+me\b[^.!?]{{0,160}}{CONFIG_NAME}',
        rf'\b(?:looking|searching)\s+for\b[^.!?]{{0,160}}{CONFIG_NAME}',
        (
            r'\b(?:i|we)\s+(?:need|want)\s+(?!(?:(?:some|sum)\s+)?help\b|to\b)'
            rf'[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
        rf'\b(?:can|could)\s+i\s+(?:get|have)\b[^.!?]{{0,160}}{CONFIG_NAME}',
        (
            r'\b(?:is|are)\s+there\s+'
            r'(?!(?:(?:a|an|any|some)\s+)?(?:reason|problem|issue|way|fix)\b)'
            rf'[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
        (
            r'\bwhere\s+(?:(?:can|do)\s+(?:i|we)\s+|to\s+)?'
            rf'(?:find|get|download)\b[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
    )
)


# Help and activity tracking
FLEASION_HELP_CHANNEL_ID = 1495014874831655052  # help-chat
FLEASION_CONFIG_CHANNEL_ID = 1463234573797167358  # configs
FLEASION_HELP_CHANNEL_IDS = frozenset(
    (
        1495010741940654182,  # general
        1548397150520737802  # fleabot-help-testing-input
        if settings.is_fleabot
        else 1548748199857225771,  # moist-help-testing-input
    )
)
HELP_TEST_CHANNEL = 1549081891225866310 if settings.is_fleabot else 1548748231242940436
CONFIG = TrackingConfig(
    activity_window=120 if settings.is_fleabot else 1,
    activity_threshold=2,
    help_window=120 if settings.is_fleabot else 1,
    help_skip_count=2,
    repost_window=600,
    repost_similarity=0.9,
    repost_min_length=20,
    warning_window=120,
)


# Channel cleanup
FLEASION_GUILD_ID = 1309760132770693181
FLEASION_CLEANUP_CHANNEL_IDS = frozenset(
    (
        1309904275932975214,  # moderation-logs
    )
)


def is_config_request(content: str) -> bool:
    """Recognize requests to obtain configs rather than questions about using them."""
    content = ' '.join(content.split())
    return any(
        pattern.search(content) is not None for pattern in CONFIG_REQUEST_PATTERNS
    )


def is_help_request(content: str) -> bool:
    """Recognize common help requests without matching every mention of help."""
    # Mentions often precede questions and should not hide the start of the request
    content = re.sub(r'<@!?\d+>', ' ', content)
    content = ' '.join(content.split())
    return any(pattern.search(content) is not None for pattern in HELP_REQUEST_PATTERNS)


class Fleasion(commands.Cog):
    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

        namespace = 'fleabot:fleasion' if settings.is_fleabot else 'moistbot:fleasion'
        self.tracking = TrackingService(bot.db_session_maker, namespace, config=CONFIG)
        self.is_testing: bool = False

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
        self.REPOST_MESSAGE_PARTIAL = (
            'Please do not repost your help message here. '
            f'Continue in {self.help_channel.mention} '
            'and wait for a reply to [your original message]({}).'
        )

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{CRICKET}')

    async def cog_load(self) -> None:
        # Restore unexpired state before the cog starts receiving messages
        await self.tracking.load()

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
        config_request = is_config_request(message.content)
        if not config_request and not is_help_request(message.content):
            return False

        if await self.tracking.check_help_cooldown(message):
            return True

        if await self.tracking.is_active(message):
            return True

        reply = self.CONFIG_MESSAGE if config_request else self.HELP_MESSAGE
        await self._send_reply(reply, message)
        return True

    @staticmethod
    def _member_has_level_role(member: discord.Member) -> bool:
        """Return whether the user has a level role."""
        # Level role (e.g. "Meow [L1]")
        return any('[' in role.name for role in member.roles)

    async def _send_reply(self, reply: str, message: discord.Message) -> None:
        """Send a test preview with the original message, or reply to the user."""
        if self.is_testing:
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

from __future__ import annotations

import datetime
import logging
import operator
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

import discord
from discord import app_commands
from discord.ext import commands

from moist_bot.utils.formats import plural

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context, GuildContext


log = logging.getLogger('discord.' + __name__)

BULK_DELETE_LIMIT = datetime.timedelta(days=14)


# ------------------------------------------------------------------
# Converters
# ------------------------------------------------------------------


class Snowflake:
    """Converter that accepts a raw Discord snowflake ID."""

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> int:
        try:
            return int(argument)
        except ValueError:
            param = ctx.current_parameter
            name = param.name if param else 'argument'
            msg = f'{name} expected a Discord ID, not {argument!r}'
            raise commands.BadArgument(msg) from None


# ------------------------------------------------------------------
# Stateful purger
# ------------------------------------------------------------------


class ChannelPurger:
    """Handles message collection and deletion for a guild channel.

    Automatically splits messages by age:
    - Recent (<14 days): bulk-deleted in chunks of 100
    - Old (>=14 days): deleted individually with rate-limit spacing
    """

    def __init__(
        self,
        channel: discord.TextChannel
        | discord.VoiceChannel
        | discord.Thread
        | discord.StageChannel,
        *,
        before: discord.abc.Snowflake | None = None,
        after: discord.abc.Snowflake | None = None,
    ) -> None:
        self.channel = channel
        self.before = before
        self.after = after
        self.deleted: list[discord.Message] = []

    async def _delete_single(self, msg: discord.Message) -> bool:
        """Remove one message. Returns False on a hard failure (caller should stop)."""
        try:
            await msg.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return False

        self.deleted.append(msg)
        return True

    async def _bulk_delete(self, messages: list[discord.Message]) -> None:
        """Bulk-delete in chunks of 100, falling back to individual deletion."""
        for i in range(0, len(messages), 100):
            chunk = messages[i : i + 100]
            try:
                if len(chunk) == 1:
                    await chunk[0].delete()
                else:
                    await self.channel.delete_messages(chunk)  # type: ignore[union-attr]
                self.deleted.extend(chunk)
            except discord.HTTPException:
                for msg in chunk:
                    if not await self._delete_single(msg):
                        return

    async def purge(
        self,
        limit: int,
        check: Callable[[discord.Message], bool] = lambda _: True,
    ) -> list[discord.Message]:
        """Collect and delete up to *limit* messages matching *check*."""
        now = discord.utils.utcnow()
        bulk_cutoff = now - BULK_DELETE_LIMIT

        messages: list[discord.Message] = []
        scan_limit = min(limit * 5, 5000)

        async for message in self.channel.history(
            limit=scan_limit, before=self.before, after=self.after
        ):
            if check(message):
                messages.append(message)
                if len(messages) >= limit:
                    break

        if not messages:
            return self.deleted

        bulk_msgs = [m for m in messages if m.created_at > bulk_cutoff]
        old_msgs = [m for m in messages if m.created_at <= bulk_cutoff]

        await self._bulk_delete(bulk_msgs)
        for msg in old_msgs:
            if not await self._delete_single(msg):
                break

        return self.deleted


# ------------------------------------------------------------------
# Flag converters
# ------------------------------------------------------------------


class PurgeFlags(commands.FlagConverter):
    before: Annotated[int | None, Snowflake] = commands.flag(
        description='Search for messages before this message ID',
        default=None,
    )
    after: Annotated[int | None, Snowflake] = commands.flag(
        description='Search for messages after this message ID',
        default=None,
    )

    def get_before(self) -> discord.Object | None:
        return discord.Object(id=self.before) if self.before else None

    def get_after(self) -> discord.Object | None:
        return discord.Object(id=self.after) if self.after else None


class TextPurgeFlags(PurgeFlags):
    text: str = commands.flag(description='Text to search for')
    limit: int = commands.flag(
        default=100, description='Max messages to search through (1-2000)'
    )


class RegexPurgeFlags(PurgeFlags):
    limit: int = commands.flag(description='Max messages to search through (1-2000)')
    pattern: str = commands.flag(description='Regex pattern to match against content')


class WebhookPurgeFlags(PurgeFlags):
    webhook: Annotated[int | None, Snowflake] = commands.flag(
        default=None, description='Specific webhook ID to filter by'
    )


# ------------------------------------------------------------------
# Cog
# ------------------------------------------------------------------


class Purge(commands.Cog):  # noqa: PLR0904
    """Bulk message deletion with various filters."""

    def __init__(self, client: MoistBot):
        self.client: MoistBot = client

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{WASTEBASKET}')

    async def cog_check(self, ctx: GuildContext) -> bool:  # type: ignore[override]
        if ctx.guild is None:  # type: ignore[unreachable]
            raise commands.NoPrivateMessage

        perms = ctx.channel.permissions_for(ctx.author)
        if not perms.manage_messages:
            raise commands.MissingPermissions(['manage_messages'])

        bot_perms = ctx.channel.permissions_for(ctx.guild.me)
        if not bot_perms.manage_messages:
            raise commands.BotMissingPermissions(['manage_messages'])
        if not bot_perms.read_message_history:
            raise commands.BotMissingPermissions(['read_message_history'])

        return True

    async def _prepare(self, ctx: GuildContext) -> None:
        """Defer the interaction or delete the invoking text message."""
        if ctx.interaction:
            if not ctx.interaction.response.is_done():
                await ctx.defer()
        else:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    def _make_purger(
        self,
        ctx: GuildContext,
        flags: PurgeFlags | None = None,
        *,
        before: discord.abc.Snowflake | None = None,
        after: discord.abc.Snowflake | None = None,
    ) -> ChannelPurger:
        """Create a ChannelPurger from context and optional flags."""
        if flags is not None:
            before = before or flags.get_before()
            after = after or flags.get_after()

        return ChannelPurger(
            ctx.channel,
            before=before or ctx.message,
            after=after,
        )

    async def _send_result(
        self, ctx: GuildContext, deleted: list[discord.Message]
    ) -> None:
        """Send an embed summarizing what was purged."""
        count = len(deleted)
        if count == 0:
            await ctx.send(
                ':warning: No messages found matching the criteria.',
                delete_after=5,
            )
            return

        authors: dict[str, int] = {}
        for msg in deleted:
            name = msg.author.display_name
            authors[name] = authors.get(name, 0) + 1

        sorted_authors = sorted(
            authors.items(), key=operator.itemgetter(1), reverse=True
        )
        breakdown = '\n'.join(f'**{name}**: {n}' for name, n in sorted_authors[:10])
        if len(sorted_authors) > 10:
            breakdown += f'\n*...and {len(sorted_authors) - 10} more*'

        embed = discord.Embed(
            description=f'Successfully removed **{plural(count):message}**.',
            color=discord.Color(0xA8B9CD),
        )
        if breakdown:
            embed.add_field(name='Breakdown', value=breakdown, inline=False)

        await ctx.send(embed=embed, delete_after=10)

    async def _validate_and_purge(
        self,
        ctx: GuildContext,
        limit: int,
        check: Callable[[discord.Message], bool] = lambda _: True,
        *,
        flags: PurgeFlags | None = None,
        before: discord.abc.Snowflake | None = None,
        after: discord.abc.Snowflake | None = None,
        confirm_threshold: int = 100,
    ) -> None:
        """Shared validation, confirmation, deletion, and feedback."""
        if limit < 1 or limit > 2000:
            await ctx.reply(':warning: Limit must be between 1 and 2000.')
            return

        if limit > confirm_threshold:
            confirm = await ctx.prompt(
                f'Are you sure you want to purge up to **{limit}** messages?'
            )
            if not confirm:
                await ctx.reply(':no_entry: Cancelled.')
                return

        await self._prepare(ctx)

        purger = self._make_purger(ctx, flags, before=before, after=after)
        deleted = await purger.purge(limit, check)

        await self._send_result(ctx, deleted)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_group(fallback='all')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(limit='Number of messages to remove (1-2000)')
    async def purge(self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags):
        """Remove messages from the channel.

        If no subcommand is given, removes the last *limit* messages.
        Requires **Manage Messages**.
        """
        await self._validate_and_purge(ctx, limit, flags=flags)

    @purge.command()
    @app_commands.describe(
        member='The user whose messages to remove',
        limit='Number of messages to search through (1-2000)',
    )
    async def user(
        self,
        ctx: GuildContext,
        member: discord.Member,
        limit: int = 100,
        *,
        flags: PurgeFlags,
    ):
        """Remove messages from a specific user."""
        await self._validate_and_purge(
            ctx, limit, check=lambda m: m.author.id == member.id, flags=flags
        )

    @purge.command()
    @app_commands.describe(
        bot='Specific bot to filter by (omit for all bots)',
        limit='Number of messages to search through (1-2000)',
    )
    async def bots(
        self,
        ctx: GuildContext,
        bot: discord.Member | None = None,
        limit: int = 100,
        *,
        flags: PurgeFlags,
    ):
        """Remove messages sent by bots."""
        if bot is not None and not bot.bot:
            await ctx.reply(f':warning: {bot.mention} is not a bot.', ephemeral=True)
            return

        def check(m: discord.Message) -> bool:
            if bot is not None:
                return m.author.id == bot.id
            return m.author.bot

        await self._validate_and_purge(ctx, limit, check=check, flags=flags)

    @purge.command()
    @app_commands.describe(limit='Number of messages to search through (1-2000)')
    async def humans(self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags):
        """Remove messages sent by humans."""
        await self._validate_and_purge(
            ctx, limit, check=lambda m: not m.author.bot, flags=flags
        )

    @purge.command()
    @app_commands.describe(limit='Number of messages to search through (1-2000)')
    async def webhooks(
        self, ctx: GuildContext, limit: int = 100, *, flags: WebhookPurgeFlags
    ):
        """Remove messages sent by webhooks."""
        wh_id = flags.webhook

        def check(m: discord.Message) -> bool:
            if wh_id is not None:
                return m.webhook_id == wh_id
            # Exclude interaction responses from the webhook filter
            return m.webhook_id is not None and m.interaction is None

        await self._validate_and_purge(ctx, limit, check=check, flags=flags)

    @purge.command()
    @app_commands.describe(limit='Number of messages to search through (1-2000)')
    async def embeds(self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags):
        """Remove messages containing embeds."""
        await self._validate_and_purge(
            ctx, limit, check=lambda m: len(m.embeds) > 0, flags=flags
        )

    @purge.command()
    @app_commands.describe(limit='Number of messages to search through (1-2000)')
    async def files(self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags):
        """Remove messages with attachments."""
        await self._validate_and_purge(
            ctx, limit, check=lambda m: len(m.attachments) > 0, flags=flags
        )

    @purge.command()
    @app_commands.describe(limit='Number of messages to search through (1-2000)')
    async def images(self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags):
        """Remove messages with image attachments or image embeds."""

        def check(msg: discord.Message) -> bool:
            return any(
                a.content_type is not None and a.content_type.startswith('image/')
                for a in msg.attachments
            ) or any(e.type == 'image' for e in msg.embeds)

        await self._validate_and_purge(ctx, limit, check=check, flags=flags)

    @purge.command()
    async def contains(self, ctx: GuildContext, *, flags: TextPurgeFlags):
        """Remove messages containing a substring (case-insensitive)."""
        lowered = flags.text.lower()
        await self._validate_and_purge(
            ctx, flags.limit, check=lambda m: lowered in m.content.lower(), flags=flags
        )

    @purge.command()
    async def startswith(self, ctx: GuildContext, *, flags: TextPurgeFlags):
        """Remove messages starting with a string (case-insensitive)."""
        lowered = flags.text.lower()
        await self._validate_and_purge(
            ctx,
            flags.limit,
            check=lambda m: m.content.lower().startswith(lowered),
            flags=flags,
        )

    @purge.command()
    async def endswith(self, ctx: GuildContext, *, flags: TextPurgeFlags):
        """Remove messages ending with a string (case-insensitive)."""
        lowered = flags.text.lower()
        await self._validate_and_purge(
            ctx,
            flags.limit,
            check=lambda m: m.content.lower().endswith(lowered),
            flags=flags,
        )

    @purge.command()
    @app_commands.describe(limit='Number of messages to search through (1-2000)')
    async def links(self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags):
        """Remove messages containing URLs."""
        url_re = re.compile(r'https?://\S+')
        await self._validate_and_purge(
            ctx, limit, check=lambda m: bool(url_re.search(m.content)), flags=flags
        )

    @purge.command()
    @app_commands.describe(limit='Number of messages to search through (1-2000)')
    async def mentions(self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags):
        """Remove messages that mention users."""
        await self._validate_and_purge(
            ctx, limit, check=lambda m: len(m.mentions) > 0, flags=flags
        )

    @purge.command()
    @app_commands.describe(limit='Number of messages to search through (1-2000)')
    async def emoji(self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags):
        """Remove messages that consist entirely of emoji."""
        custom_re = re.compile(r'<a?:\w+:\d+>')
        unicode_re = re.compile(
            r'[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff'
            r'\U0001f680-\U0001f6ff\U0001f1e0-\U0001f1ff'
            r'\U00002702-\U000027b0\U0000fe00-\U0000fe0f'
            r'\U0001f900-\U0001f9ff\U0001fa00-\U0001fa6f'
            r'\U0001fa70-\U0001faff\U00002600-\U000026ff'
            r'\U0000200d\U0000fe0f\s]+'
        )

        def check(msg: discord.Message) -> bool:
            content = msg.content.strip()
            if not content:
                return False
            content = custom_re.sub('', content)
            content = unicode_re.sub('', content)
            return not content.strip()

        await self._validate_and_purge(ctx, limit, check=check, flags=flags)

    @purge.command()
    @app_commands.describe(limit='Number of messages to scan (1-2000)')
    async def reactions(
        self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags
    ):
        """Clear all reactions from recent messages (does not delete them)."""
        if limit < 1 or limit > 2000:
            await ctx.reply(':warning: Limit must be between 1 and 2000.')
            return

        await self._prepare(ctx)

        before = flags.get_before() or ctx.message
        after = flags.get_after()

        count = 0
        async with ctx.typing():
            async for message in ctx.channel.history(
                limit=limit, before=before, after=after
            ):
                if message.reactions:
                    try:
                        await message.clear_reactions()
                        count += 1
                    except discord.HTTPException:
                        pass

        await ctx.send(
            f':white_check_mark: Cleared reactions from **{plural(count):message}**.',
            delete_after=5,
        )

    @purge.command()
    @app_commands.describe(limit='Number of messages to search through (1-2000)')
    async def pins(self, ctx: GuildContext, limit: int = 100, *, flags: PurgeFlags):
        """Remove non-pinned messages (clean a channel while preserving pins)."""
        await self._validate_and_purge(
            ctx, limit, check=lambda m: not m.pinned, flags=flags
        )

    @purge.command(name='after', with_app_command=False)
    async def purge_after(self, ctx: GuildContext, message: discord.Message):
        """Remove all messages after a given message ID or link (up to 2000)."""
        confirm = await ctx.prompt(
            'This will delete up to **2000** messages after the specified message. Continue?'
        )
        if not confirm:
            return await ctx.reply(':no_entry: Cancelled.')

        await self._prepare(ctx)

        purger = ChannelPurger(ctx.channel, after=message)
        async with ctx.typing():
            deleted = await purger.purge(2000)

        await self._send_result(ctx, deleted)

    @purge.command(name='before', with_app_command=False)
    async def purge_before(
        self, ctx: GuildContext, message: discord.Message, limit: int = 100
    ):
        """Remove messages before a given message ID or link."""
        await self._validate_and_purge(ctx, limit, before=message)

    @purge.command(with_app_command=False)
    async def between(
        self,
        ctx: GuildContext,
        start: discord.Message,
        end: discord.Message,
    ):
        """Remove all messages between two message IDs or links (up to 2000)."""
        if start.created_at > end.created_at:
            start, end = end, start

        confirm = await ctx.prompt(
            'This will delete up to **2000** messages between the two messages. Continue?'
        )
        if not confirm:
            return await ctx.reply(':no_entry: Cancelled.')

        await self._prepare(ctx)

        purger = ChannelPurger(ctx.channel, after=start, before=end)
        async with ctx.typing():
            deleted = await purger.purge(2000)

        await self._send_result(ctx, deleted)

    @purge.command()
    async def regex(self, ctx: GuildContext, *, flags: RegexPurgeFlags):
        """Remove messages matching a regex pattern."""
        try:
            compiled = re.compile(flags.pattern)
        except re.error as e:
            await ctx.reply(f':warning: Invalid regex: `{e}`')
            return

        await self._validate_and_purge(
            ctx,
            flags.limit,
            check=lambda m: bool(compiled.search(m.content)),
            flags=flags,
        )


async def setup(client: MoistBot) -> None:
    await client.add_cog(Purge(client))

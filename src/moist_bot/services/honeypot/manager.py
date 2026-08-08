from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, cast

import discord
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from moist_bot.models import (
    GuildHoneypotConfig,
    HoneypotGuildStats,
    HoneypotIncident,
    HoneypotUserStats,
)
from moist_bot.utils.converters import shorten
from moist_bot.utils.formats import plural

from .config import HoneypotConfig
from .constants import (
    BAN_TRIGGER_MODULO,
    CONTENT_EXCERPT_WIDTH,
    PUNISHMENT_REASON,
    SOFTBAN_DELETE_MESSAGE_SECONDS,
)
from .message_bloom import MessageBloomFilter
from .scanner import MessageScanner
from .types import (
    GuildMessage,
    HoneypotPunishmentAction,
    HoneypotScanResult,
    Punishment,
)

if TYPE_CHECKING:
    from typing import Any

    from moist_bot.bot import MoistBot


log = logging.getLogger('discord.' + __name__)


class HoneypotManager:
    """Manage persistent honeypot config and incident records."""

    def __init__(self, bot: MoistBot) -> None:
        self.bot: MoistBot = bot

        # Cache
        self._incident_counts: defaultdict[int, int] = defaultdict(int)
        self._user_incident_counts: defaultdict[tuple[int, int], int] = defaultdict(int)

        # Components
        self.config: HoneypotConfig = HoneypotConfig(self)
        self.handled_message_bloom: MessageBloomFilter = MessageBloomFilter()
        self.message_scanner: MessageScanner = MessageScanner(self)

        # Locks and tasks
        self._rebuild_bloom_task: asyncio.Task[Any] | None = None
        self._load_lock: asyncio.Lock = asyncio.Lock()
        self._trigger_locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def load(self) -> None:
        """Load honeypot state into memory."""

        async with self._load_lock:
            await self.config.load()
            async with self.bot.db_session_maker() as session:
                incident_counts = await HoneypotGuildStats.counts_by_guild(session)
                user_counts_result = await session.execute(select(HoneypotUserStats))
                user_counts = {
                    (row.guild_id, row.user_id): row.total_incidents
                    for row in user_counts_result.scalars().all()
                }
                incidents_result = await session.execute(select(HoneypotIncident))
                handled_message_keys = [
                    (incident.guild_id, incident.message_id)
                    for incident in incidents_result.scalars().all()
                ]

            self._incident_counts = defaultdict(int, incident_counts)
            self._user_incident_counts = defaultdict(int, user_counts)
            self.handled_message_bloom = MessageBloomFilter(
                expected_items=sum(incident_counts.values())
            )

            for guild_id, message_id in handled_message_keys:
                self.handled_message_bloom.add(
                    guild_id=guild_id,
                    message_id=message_id,
                )

    def incident_count_for_guild(self, *, guild_id: int) -> int:
        """Return the total number of honeypot incidents for a guild."""
        return self._incident_counts[guild_id]

    def incident_user_ids_for_guild(self, *, guild_id: int) -> frozenset[int]:
        """Return user IDs with honeypot incidents for a guild."""
        return frozenset(
            user_id
            for (
                incident_guild_id,
                user_id,
            ), count in self._user_incident_counts.items()
            if incident_guild_id == guild_id and count > 0
        )

    async def handle_message(self, message: discord.Message) -> None:
        """Handle a live message sent to a configured honeypot channel."""

        # Early reject conditions
        if await self.is_exempt(message):
            return

        # This is confirmed by `self.is_exempt`
        if TYPE_CHECKING:
            message = cast('GuildMessage', message)

        # Config reject conditions
        config = self.config.get(message.guild.id)
        if (
            config is None
            or message.channel.id != config.channel_id
            or not config.enabled
        ):
            return

        # Only non-exempt human members reaching this point trigger punishment
        await self._handle_trigger(
            message=message,
            config=config,
        )

    async def scan_guild(
        self, guild_id: int, *, ignore_disabled: bool = False
    ) -> HoneypotScanResult:
        """Scan the configured honeypot channel for one guild."""
        return await self.message_scanner.scan_guild(
            guild_id,
            ignore_disabled=ignore_disabled,
        )

    async def scan_enabled_configs(self) -> HoneypotScanResult:
        """Scan all enabled honeypot configs."""
        return await self.message_scanner.scan_enabled_configs()

    def start_scan_once(self) -> None:
        """Start the one-time automatic honeypot scan."""
        self.message_scanner.start_scan_once()

    def mark_scan_once_done(self) -> None:
        """Mark the automatic scan as already handled for this process."""
        self.message_scanner.mark_scan_once_done()

    def cancel_scan(self) -> None:
        """Cancel a pending automatic scan."""
        self.message_scanner.cancel_scan()

        if self._rebuild_bloom_task is not None and not self._rebuild_bloom_task.done():
            self._rebuild_bloom_task.cancel()

    async def is_exempt(self, message: discord.Message) -> bool:
        """Return whether a message should bypass automatic honeypot action."""

        author = message.author
        return (
            message.guild is None
            or message.webhook_id is not None
            or await self.bot.is_owner(author)
            or not isinstance(author, discord.Member)
            or author == message.guild.me
            or author.guild_permissions.manage_guild
            or author.guild_permissions.administrator
        )

    async def recorded_message_ids(
        self, guild_id: int, message_ids: list[int]
    ) -> set[int]:
        """Return already-recorded honeypot message IDs."""

        maybe_recorded_ids = self.handled_message_bloom.maybe_contained_ids(
            guild_id=guild_id,
            message_ids=message_ids,
        )
        if not maybe_recorded_ids:
            return set()

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(HoneypotIncident.message_id).where(
                    col(HoneypotIncident.guild_id) == guild_id,
                    col(HoneypotIncident.message_id).in_(maybe_recorded_ids),
                )
            )
            return set(result.scalars().all())

    def next_trigger_count(self, guild_id: int, user_id: int) -> int:
        """Return the next trigger count from the in-memory user stats."""
        return self._user_incident_counts[guild_id, user_id] + 1

    async def log_and_record_trigger(
        self,
        message: GuildMessage,
        member: discord.Member,
        config: GuildHoneypotConfig,
        punishment: Punishment,
    ) -> bool:
        """Send the incident log and record the final outcome."""

        incident = HoneypotIncident(
            config_id=config.id,
            guild_id=config.guild_id,
            channel_id=config.channel_id,
            log_channel_id=config.log_channel_id,
            user_id=member.id,
            message_id=message.id,
            message_created_at=message.created_at,
            content_excerpt=self._content_excerpt(message),
            attachment_count=len(message.attachments),
            trigger_count=punishment.trigger_count,
            delete_message_seconds=punishment.delete_message_seconds,
            punishment_action=punishment.action.value,
            punishment_succeeded=punishment.succeeded,
            punishment_error=punishment.error,
        )
        log_sent, log_error = await self._send_log_embed(
            incident=incident,
            message=message,
            member=member,
        )
        incident.log_sent = log_sent
        incident.log_error = log_error
        return await self._create_incident(incident)

    async def punish_member(
        self,
        member: discord.Member,
        *,
        trigger_count: int,
        delete_message_seconds: int = SOFTBAN_DELETE_MESSAGE_SECONDS,
    ) -> Punishment:
        """Punish a member according to their honeypot trigger history."""

        action = (
            HoneypotPunishmentAction.BAN
            if trigger_count % BAN_TRIGGER_MODULO == 0
            else HoneypotPunishmentAction.SOFTBAN
        )

        succeeded: bool = True
        error: str | None = None
        ban_applied: bool = True

        if action is HoneypotPunishmentAction.BAN:
            try:
                await member.ban(
                    reason=PUNISHMENT_REASON,
                    delete_message_seconds=delete_message_seconds,
                )
            except discord.HTTPException as e:
                log.warning(
                    f'Failed to ban user {member} ({member.id}) '
                    f'in guild {member.guild} ({member.guild.id}): {e}'
                )
                error = shorten(f'Ban failed: {e}', CONTENT_EXCERPT_WIDTH)
                succeeded = False
                ban_applied = False

        elif action is HoneypotPunishmentAction.SOFTBAN:
            result = await self.bot.softban_member(
                member,
                reason=PUNISHMENT_REASON,
                delete_message_seconds=delete_message_seconds,
            )
            succeeded = result.softbanned
            error = (
                shorten(result.error, CONTENT_EXCERPT_WIDTH) if result.error else None
            )
            ban_applied = result.ban_applied

        return Punishment(
            action=action,
            trigger_count=trigger_count,
            delete_message_seconds=delete_message_seconds,
            succeeded=succeeded,
            error=error,
            ban_applied=ban_applied,
        )

    async def _handle_trigger(
        self, *, message: GuildMessage, config: GuildHoneypotConfig
    ) -> None:
        """Run the full punishment flow for a honeypot trigger."""

        if (
            await self._message_was_handled(
                guild_id=config.guild_id, message_id=message.id
            )
            or message.author.bot  # Special case for bots
        ):
            await self._delete_honeypot_message(message)
            return

        lock = self._trigger_locks[config.guild_id, message.author.id]
        if lock.locked():
            await self._delete_honeypot_message(message)
            log.debug(
                f'Deleted duplicate live honeypot message {message.id} '
                f'from {message.author} ({message.author.id}) while a trigger '
                f'was already being handled.'
            )
            return

        async with lock:
            trigger_count = self.next_trigger_count(config.guild_id, message.author.id)
            punishment = await self.punish_member(
                message.author, trigger_count=trigger_count
            )
            await self.log_and_record_trigger(
                message,
                message.author,
                config,
                punishment,
            )

    async def _delete_honeypot_message(self, message: GuildMessage) -> bool:
        """Deletes one live honeypot message without recording a new incident."""

        try:
            await message.delete()
        except discord.NotFound:
            return True
        except discord.HTTPException as e:
            log.warning(
                f'Failed to delete handled honeypot message {message.id} '
                f'in guild {message.guild} ({message.guild.id}): {e}'
            )
            return False
        return True

    async def _create_incident(self, incident: HoneypotIncident) -> bool:
        """Record one completed incident and update stats."""

        if await self._message_was_handled(
            guild_id=incident.guild_id, message_id=incident.message_id
        ):
            return False

        async with self.bot.db_session_maker() as session:
            await HoneypotGuildStats.increment(session, guild_id=incident.guild_id)
            await HoneypotUserStats.increment(
                session, guild_id=incident.guild_id, user_id=incident.user_id
            )
            session.add(incident)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False

        await self._rebuild_handled_message_bloom()
        self._mark_incident_handled(incident)
        return True

    async def _send_log_embed(
        self,
        *,
        incident: HoneypotIncident,
        message: GuildMessage,
        member: discord.Member,
    ) -> tuple[bool, str | None]:
        """Send the configured incident log embed."""

        try:
            channel = await self.bot.get_or_fetch_channel(incident.log_channel_id)
        except discord.HTTPException as e:
            return False, shorten(str(e), CONTENT_EXCERPT_WIDTH)

        if not hasattr(channel, 'send'):
            return False, 'Configured log channel cannot receive messages.'
        channel = cast('discord.abc.Messageable', channel)

        action = incident.punishment_action.title()
        embed = (
            discord.Embed(
                title=f'\N{HONEY POT} Honeypot Triggered - {action}',
                colour=discord.Colour.red(),
                timestamp=incident.triggered_at,
                description=incident.content_excerpt,
            )
            .set_author(
                name=f'{member.name} ({member.id})', icon_url=member.display_avatar.url
            )
            .set_footer(text=f'{plural(incident.trigger_count):trigger} from user')
        )

        if incident.attachment_count > 0:
            embed.add_field(
                name='Attachments',
                value=str(incident.attachment_count),
            )

        if incident.punishment_error is not None:
            embed.add_field(
                name='Punishment Error',
                value=incident.punishment_error,
                inline=False,
            )

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning(f'Failed to send log embed: {e}')
            return False, shorten(str(e), CONTENT_EXCERPT_WIDTH)

        log.debug(
            f'Handled honeypot trigger from {member} ({member.id}) '
            f'in guild {message.guild} ({message.guild.id}), '
            f'message {message.id}.'
        )
        return True, None

    @staticmethod
    def _content_excerpt(message: GuildMessage) -> str | None:
        """Return a bounded content excerpt for logs and incident storage."""

        content = message.content.strip()
        if not content:
            return None
        return shorten(content, CONTENT_EXCERPT_WIDTH)

    async def _message_was_handled(self, *, guild_id: int, message_id: int) -> bool:
        """Return whether a message is already represented in incident history."""

        if not self.handled_message_bloom.might_contain(
            guild_id=guild_id, message_id=message_id
        ):
            return False

        async with self.bot.db_session_maker() as session:
            incident_id = await session.scalar(
                select(HoneypotIncident.id)
                .where(
                    col(HoneypotIncident.guild_id) == guild_id,
                    col(HoneypotIncident.message_id) == message_id,
                )
                .limit(1)
            )
            return incident_id is not None

    def _mark_incident_handled(self, incident: HoneypotIncident) -> None:
        """Reflect a recorded incident in the in-memory caches."""

        self._incident_counts[incident.guild_id] += 1
        user_key = (incident.guild_id, incident.user_id)
        self._user_incident_counts[user_key] = max(
            self._user_incident_counts[user_key], incident.trigger_count
        )

        self.handled_message_bloom.add(
            guild_id=incident.guild_id, message_id=incident.message_id
        )

    async def _rebuild_handled_message_bloom(self) -> None:
        """Rebuild the handled-message Bloom filter at double current size."""

        if self._rebuild_bloom_task is not None and not self._rebuild_bloom_task.done():
            await self._rebuild_bloom_task
            return

        if not self.handled_message_bloom.over_capacity():
            return

        async def _rebuild() -> None:
            async with self.bot.db_session_maker() as session:
                result = await session.execute(select(HoneypotIncident))
                keys = [
                    (incident.guild_id, incident.message_id)
                    for incident in result.scalars().all()
                ]

            bloom = MessageBloomFilter(expected_items=len(keys) * 2)
            for guild_id, message_id in keys:
                bloom.add(guild_id=guild_id, message_id=message_id)
            self.handled_message_bloom = bloom

        async def _task() -> None:
            try:
                await _rebuild()
            finally:
                if self._rebuild_bloom_task is asyncio.current_task():
                    self._rebuild_bloom_task = None

        self._rebuild_bloom_task = asyncio.create_task(_task())

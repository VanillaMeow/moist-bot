from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING, cast

import discord
from fastbloom_rs import BloomFilter
from sqlalchemy import update
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
from moist_bot.utils.message_purge import ChannelPurger

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any

    from moist_bot.bot import MoistBot

    class GuildMessage(discord.Message):
        """A message in a guild."""

        guild: discord.Guild
        author: discord.Member  # type: ignore[reportIncompatibleVariableOverride]


log = logging.getLogger('discord.' + __name__)


CONTENT_EXCERPT_WIDTH = 500
PUNISHMENT_REASON = 'Triggered honeypot channel.'
SOFTBAN_DELETE_MESSAGE_SECONDS = 5 * 60
DISCORD_MAX_DELETE_MESSAGE_SECONDS = 7 * 24 * 60 * 60
SCAN_DELETE_SECONDS_GRACE = 5
BAN_TRIGGER_MODULO = 3
BLOOM_MIN_EXPECTED_ITEMS = 1024


class HoneypotPunishmentAction(StrEnum):
    SOFTBAN = auto()
    BAN = auto()


class MessageBloomFilter:
    """Bounded-memory maybe-set for handled honeypot messages."""

    __slots__ = ('bloom', 'expected_items', 'inserted_items')

    def __init__(self, *, expected_items: int = 0, false_positive_rate: float = 0.01):
        self.expected_items: int = max(BLOOM_MIN_EXPECTED_ITEMS, expected_items * 2)
        self.inserted_items: int = 0
        self.bloom: BloomFilter = BloomFilter(self.expected_items, false_positive_rate)

    @staticmethod
    def _key(*, guild_id: int, message_id: int) -> bytes:
        return f'{guild_id}:{message_id}'.encode()

    def add(self, *, guild_id: int, message_id: int) -> None:
        """Add a handled message key to the filter."""
        self.bloom.add_bytes(self._key(guild_id=guild_id, message_id=message_id))
        self.inserted_items += 1

    def over_capacity(self) -> bool:
        """Return whether the filter is at or has outgrown its expected item count."""
        return self.inserted_items >= self.expected_items

    def might_contain(self, *, guild_id: int, message_id: int) -> bool:
        """Return whether a handled message key may be in the filter."""
        return self.bloom.contains_bytes(
            self._key(guild_id=guild_id, message_id=message_id)
        )

    def maybe_contained_ids(
        self,
        *,
        guild_id: int,
        message_ids: list[int],
    ) -> list[int]:
        """Return message IDs that may be in the filter."""

        keys = [
            self._key(guild_id=guild_id, message_id=message_id)
            for message_id in message_ids
        ]
        results = self.bloom.contains_bytes_batch(keys, check_type=False)
        return [
            message_id
            for message_id, maybe_present in zip(
                message_ids,
                results,
                strict=True,
            )
            if maybe_present
        ]


@dataclass(slots=True)
class HoneypotScanBatch:
    """Messages found for one member during a honeypot scan."""

    member: discord.Member
    messages: list[GuildMessage]


@dataclass(frozen=True, slots=True)
class HoneypotScanBatchResult:
    """Result from handling one scanned member batch."""

    messages_deleted: int = 0
    incident_recorded: bool = False


@dataclass(frozen=True, slots=True)
class Punishment:
    """Outcome of an automatic honeypot punishment."""

    action: HoneypotPunishmentAction
    trigger_count: int
    delete_message_seconds: int
    succeeded: bool
    error: str | None
    ban_applied: bool


@dataclass(slots=True)
class HoneypotScanResult:
    """Summary of a honeypot scan."""

    configs_checked: int = 0
    messages_found: int = 0
    members_handled: int = 0
    incidents_recorded: int = 0
    messages_deleted: int = 0

    def merge(self, other: HoneypotScanResult) -> None:
        """Add another scan result into this one."""

        self.configs_checked += other.configs_checked
        self.messages_found += other.messages_found
        self.members_handled += other.members_handled
        self.incidents_recorded += other.incidents_recorded
        self.messages_deleted += other.messages_deleted


class HoneypotScanAlreadyRunningError(Exception):
    """Raised when a guild already has an active honeypot scan."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id: int = guild_id
        super().__init__(f'Honeypot scan already running for guild {guild_id}.')


class HoneypotManager:
    """Manage persistent honeypot config and incident records."""

    def __init__(self, bot: MoistBot) -> None:
        self.bot: MoistBot = bot

        # Cache
        self.configs: dict[int, GuildHoneypotConfig] = {}
        self._incident_counts: defaultdict[int, int] = defaultdict(int)
        self._user_incident_counts: defaultdict[tuple[int, int], int] = defaultdict(int)
        self._handled_message_bloom: MessageBloomFilter = MessageBloomFilter()

        # Locks and tasks
        self._rebuild_bloom_task: asyncio.Task[Any] | None = None
        self._load_lock: asyncio.Lock = asyncio.Lock()
        self._scan_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

        # Initial scan once
        self._scan_once_done: bool = False
        self._scan_once_task: asyncio.Task[Any] | None = None

    async def load(self) -> None:
        """Load all honeypot configs into memory."""

        async with self._load_lock:
            async with self.bot.db_session_maker() as session:
                result = await session.execute(select(GuildHoneypotConfig))
                rows = list(result.scalars().all())
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

            self.configs.clear()
            self._incident_counts = defaultdict(int, incident_counts)
            self._user_incident_counts = defaultdict(int, user_counts)
            self._handled_message_bloom = MessageBloomFilter(
                expected_items=sum(incident_counts.values())
            )

            for row in rows:
                self.configs[row.guild_id] = row
            for guild_id, message_id in handled_message_keys:
                self._handled_message_bloom.add(
                    guild_id=guild_id,
                    message_id=message_id,
                )

            log.info(f'Loaded {len(rows)} honeypot configs.')

    def get_config(self, guild_id: int) -> GuildHoneypotConfig | None:
        """Return the cached config for a guild."""
        return self.configs.get(guild_id)

    def enabled_configs(self) -> tuple[GuildHoneypotConfig, ...]:
        """Return all enabled cached honeypot configs."""
        return tuple(config for config in self.configs.values() if config.enabled)

    async def _is_exempt(self, message: discord.Message) -> bool:
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

    @staticmethod
    def _content_excerpt(message: GuildMessage) -> str | None:
        """Return a bounded content excerpt for logs and incident storage."""

        content = message.content.strip()
        if not content:
            return None
        return shorten(content, CONTENT_EXCERPT_WIDTH)

    @staticmethod
    def _delete_seconds_for_oldest_message(message: GuildMessage) -> int:
        """Return a Discord delete window for a scanned honeypot message."""

        age = discord.utils.utcnow() - message.created_at
        seconds = int(max(0.0, age.total_seconds())) + SCAN_DELETE_SECONDS_GRACE
        return min(seconds, DISCORD_MAX_DELETE_MESSAGE_SECONDS)

    def incident_count_for_guild(self, *, guild_id: int) -> int:
        """Return the total number of honeypot incidents for a guild."""
        return self._incident_counts[guild_id]

    def _increment_incident_count(self, guild_id: int) -> None:
        """Increment the cached guild incident total."""
        self._incident_counts[guild_id] += 1

    def _next_trigger_count(self, *, guild_id: int, user_id: int) -> int:
        """Return the next trigger count from the in-memory user stats."""
        return self._user_incident_counts[guild_id, user_id] + 1

    def _mark_incident_handled(self, incident: HoneypotIncident) -> None:
        """Reflect a recorded incident in the in-memory caches."""

        self._increment_incident_count(incident.guild_id)
        user_key = (incident.guild_id, incident.user_id)
        self._user_incident_counts[user_key] = max(
            self._user_incident_counts[user_key],
            incident.trigger_count,
        )
        self._handled_message_bloom.add(
            guild_id=incident.guild_id,
            message_id=incident.message_id,
        )

    async def _rebuild_handled_message_bloom(self) -> None:
        """Rebuild the handled-message Bloom filter at double current size."""

        if self._rebuild_bloom_task is not None and not self._rebuild_bloom_task.done():
            await self._rebuild_bloom_task
            return

        if not self._handled_message_bloom.over_capacity():
            return

        # Create a new task to rebuild the filter
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
            self._handled_message_bloom = bloom

        async def _task() -> None:
            try:
                await _rebuild()
            finally:
                if self._rebuild_bloom_task is asyncio.current_task():
                    self._rebuild_bloom_task = None

        # Set new task
        self._rebuild_bloom_task = asyncio.create_task(_task())

    async def _message_was_handled(self, *, guild_id: int, message_id: int) -> bool:
        """Return whether a message is already represented in incident history."""

        if not self._handled_message_bloom.might_contain(
            guild_id=guild_id,
            message_id=message_id,
        ):
            return False

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(HoneypotIncident.id).where(
                    col(HoneypotIncident.guild_id) == guild_id,
                    col(HoneypotIncident.message_id) == message_id,
                )
            )
            return result.scalar_one_or_none() is not None

    async def set_config(
        self,
        *,
        guild_id: int,
        channel_id: int,
        log_channel_id: int,
        updated_by_id: int,
    ) -> GuildHoneypotConfig:
        """Create or update a guild honeypot config."""

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildHoneypotConfig).where(
                    col(GuildHoneypotConfig.guild_id) == guild_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                config = GuildHoneypotConfig(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    log_channel_id=log_channel_id,
                )
                session.add(config)
            elif config.channel_id != channel_id:
                config.alert_message_id = None

            config.channel_id = channel_id
            config.log_channel_id = log_channel_id
            config.enabled = True
            config.updated_at = discord.utils.utcnow()
            config.updated_by_id = updated_by_id
            await session.flush()
            await session.commit()

        self.configs[guild_id] = config
        return config

    async def set_alert_message_id(
        self,
        *,
        guild_id: int,
        alert_message_id: int,
        updated_by_id: int,
    ) -> GuildHoneypotConfig | None:
        """Updates a guild config's alert message id."""

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildHoneypotConfig).where(
                    col(GuildHoneypotConfig.guild_id) == guild_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                self.configs.pop(guild_id, None)
                return None

            config.alert_message_id = alert_message_id
            config.updated_at = discord.utils.utcnow()
            config.updated_by_id = updated_by_id
            await session.flush()
            await session.commit()

        self.configs[guild_id] = config
        return config

    async def disable_config(self, *, guild_id: int, updated_by_id: int) -> bool:
        """Disable a guild honeypot config if one exists."""

        return await self.set_config_enabled(
            guild_id=guild_id,
            enabled=False,
            updated_by_id=updated_by_id,
        )

    async def enable_config(self, *, guild_id: int, updated_by_id: int) -> bool:
        """Enable a guild honeypot config if one exists."""

        return await self.set_config_enabled(
            guild_id=guild_id,
            enabled=True,
            updated_by_id=updated_by_id,
        )

    async def set_config_enabled(
        self,
        *,
        guild_id: int,
        enabled: bool,
        updated_by_id: int,
    ) -> bool:
        """Set whether a guild honeypot config is enabled if one exists."""

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildHoneypotConfig).where(
                    col(GuildHoneypotConfig.guild_id) == guild_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                return False

            config.enabled = enabled
            config.updated_at = discord.utils.utcnow()
            config.updated_by_id = updated_by_id
            await session.flush()
            await session.commit()

        self.configs[guild_id] = config
        return True

    async def delete_config(self, *, guild_id: int) -> bool:
        """Deletes a guild honeypot config while preserving incident history."""

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildHoneypotConfig).where(
                    col(GuildHoneypotConfig.guild_id) == guild_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                self.configs.pop(guild_id, None)
                return False

            await session.execute(
                update(HoneypotIncident)
                .where(col(HoneypotIncident.guild_id) == guild_id)
                .values(config_id=None)
            )
            await session.delete(config)
            await session.commit()

        self.configs.pop(guild_id, None)
        return True

    async def _recorded_message_ids(
        self,
        *,
        guild_id: int,
        message_ids: list[int],
    ) -> set[int]:
        """Return already-recorded honeypot message IDs."""

        maybe_recorded_ids = self._handled_message_bloom.maybe_contained_ids(
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

    async def _create_incident(self, incident: HoneypotIncident) -> bool:
        """Record one completed incident and update stats."""

        if await self._message_was_handled(
            guild_id=incident.guild_id,
            message_id=incident.message_id,
        ):
            return False

        async with self.bot.db_session_maker() as session:
            await HoneypotGuildStats.increment(session, guild_id=incident.guild_id)
            await HoneypotUserStats.increment(
                session,
                guild_id=incident.guild_id,
                user_id=incident.user_id,
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

    async def _log_and_record_trigger(
        self,
        *,
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
                guild_id=config.guild_id,
                message_id=message.id,
            )
            or message.author.bot  # Special case for bots
        ):
            await self._delete_honeypot_message(message)
            return

        trigger_count = self._next_trigger_count(
            guild_id=config.guild_id,
            user_id=message.author.id,
        )
        punishment = await self.punish_member(
            message.author,
            trigger_count=trigger_count,
        )
        await self._log_and_record_trigger(
            message=message,
            member=message.author,
            config=config,
            punishment=punishment,
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

    async def handle_message(self, message: discord.Message) -> None:
        """Handle a live message sent to a configured honeypot channel."""

        # Early reject conditions
        if await self._is_exempt(message):
            return

        # This is confirmed by `self._is_exempt`
        if TYPE_CHECKING:
            message = cast('GuildMessage', message)

        # Config reject conditions
        config = self.get_config(message.guild.id)
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

    #
    # Guild scanning
    #

    async def _resolve_scan_channel(
        self,
        config: GuildHoneypotConfig,
    ) -> discord.TextChannel | None:
        """Resolve a configured honeypot scan channel."""

        try:
            guild = await self.bot.get_or_fetch_guild(config.guild_id)
        except discord.HTTPException:
            log.warning(f'Skipping honeypot scan for missing guild {config.guild_id}.')
            return None

        try:
            channel = await self.bot.get_or_fetch_channel(
                config.channel_id,
                guild=guild,
            )
        except discord.HTTPException as e:
            log.warning(
                f'Failed to fetch honeypot channel {config.channel_id} '
                f'in guild {guild} ({guild.id}): {e}'
            )
            return None

        if not isinstance(channel, discord.TextChannel):
            log.warning(
                f'Skipping honeypot scan for non-text channel '
                f'{config.channel_id} in guild {guild} ({guild.id}).'
            )
            return None

        permissions = channel.permissions_for(guild.me)
        if not permissions.read_messages or not permissions.read_message_history:
            log.warning(
                f'Skipping honeypot scan for {channel} ({channel.id}) '
                f'in guild {guild} ({guild.id}); missing history permissions.'
            )
            return None

        return channel

    async def _scan_channel(
        self,
        *,
        channel: discord.TextChannel,
        before: datetime,
    ) -> dict[int, HoneypotScanBatch]:
        """Scan a honeypot channel and group messages by member."""

        batches: dict[int, HoneypotScanBatch] = {}
        async for message in channel.history(limit=None, before=before):
            if await self._is_exempt(message):
                continue

            # These are already confirmed by `self._is_exempt`
            if TYPE_CHECKING:
                message = cast('GuildMessage', message)

            # Add the message to the member batch
            member = message.author
            batch = batches.get(member.id)
            if batch is None:
                batch = HoneypotScanBatch(member=member, messages=[])
                batches[member.id] = batch
            batch.messages.append(message)

        return batches

    async def _delete_scan_messages(
        self,
        *,
        messages: list[GuildMessage],
    ) -> int:
        """Deletes scanned honeypot messages not covered by ban deletion."""

        if not messages:
            return 0

        channel = cast('discord.abc.Messageable', messages[0].channel)
        purger = ChannelPurger(channel)
        deleted = await purger.delete_messages(cast('list[discord.Message]', messages))
        failed_count = len(messages) - len(deleted)
        if failed_count:
            log.warning(f'Failed to manually delete {failed_count} honeypot messages.')
        return len(deleted)

    @staticmethod
    def _scan_messages_requiring_manual_delete(
        *,
        messages: list[GuildMessage],
        delete_message_seconds: int,
        ban_applied: bool,
    ) -> list[GuildMessage]:
        """Return scanned messages not covered by Discord's ban deletion."""

        if not ban_applied:
            return messages

        now = discord.utils.utcnow()
        return [
            message
            for message in messages
            if (now - message.created_at).total_seconds() > delete_message_seconds
        ]

    async def _handle_scan_batch(
        self,
        *,
        config: GuildHoneypotConfig,
        batch: HoneypotScanBatch,
    ) -> HoneypotScanBatchResult:
        """Handle all scanned honeypot messages for one member."""

        if batch.member.bot:
            deleted_count = await self._delete_scan_messages(messages=batch.messages)
            return HoneypotScanBatchResult(messages_deleted=deleted_count)

        recorded_ids = await self._recorded_message_ids(
            guild_id=config.guild_id,
            message_ids=[message.id for message in batch.messages],
        )

        # TODO(leah): bruh
        recorded_messages = [
            message for message in batch.messages if message.id in recorded_ids
        ]
        unrecorded_messages = [
            message for message in batch.messages if message.id not in recorded_ids
        ]

        recorded_deleted_count = await self._delete_scan_messages(
            messages=recorded_messages
        )
        if not unrecorded_messages:
            return HoneypotScanBatchResult(messages_deleted=recorded_deleted_count)

        oldest_message = min(
            unrecorded_messages,
            key=lambda message: message.created_at,
        )
        delete_message_seconds = self._delete_seconds_for_oldest_message(oldest_message)
        trigger_count = self._next_trigger_count(
            guild_id=config.guild_id,
            user_id=batch.member.id,
        )
        punishment = await self.punish_member(
            batch.member,
            trigger_count=trigger_count,
            delete_message_seconds=delete_message_seconds,
        )
        incident_recorded = await self._log_and_record_trigger(
            message=oldest_message,
            member=batch.member,
            config=config,
            punishment=punishment,
        )

        manual_delete_messages = self._scan_messages_requiring_manual_delete(
            messages=unrecorded_messages,
            delete_message_seconds=punishment.delete_message_seconds,
            ban_applied=punishment.ban_applied,
        )
        ban_deleted_count = len(unrecorded_messages) - len(manual_delete_messages)
        manual_deleted_count = await self._delete_scan_messages(
            messages=manual_delete_messages
        )
        deleted_count = (
            recorded_deleted_count + ban_deleted_count + manual_deleted_count
        )

        if manual_deleted_count:
            log.debug(
                f'Manually deleted {manual_deleted_count} scanned honeypot messages '
                f'for {batch.member} ({batch.member.id}).'
            )
        if ban_deleted_count:
            log.debug(
                f'Discord ban deletion covered {ban_deleted_count} scanned '
                f'honeypot messages for '
                f'{batch.member} ({batch.member.id}).'
            )

        return HoneypotScanBatchResult(
            messages_deleted=deleted_count,
            incident_recorded=incident_recorded,
        )

    async def _scan_config(
        self,
        *,
        config: GuildHoneypotConfig,
        before: datetime,
    ) -> HoneypotScanResult:
        """Scan one honeypot config and return summary counts."""

        result = HoneypotScanResult(configs_checked=1)
        channel = await self._resolve_scan_channel(config)
        if channel is None:
            return result

        batches = await self._scan_channel(channel=channel, before=before)
        result.messages_found = sum(len(batch.messages) for batch in batches.values())
        for batch in batches.values():
            batch_result = await self._handle_scan_batch(
                config=config,
                batch=batch,
            )
            result.messages_deleted += batch_result.messages_deleted
            if batch_result.incident_recorded:
                result.members_handled += 1
                result.incidents_recorded += 1

        return result

    async def _scan_config_with_lock(
        self,
        *,
        config: GuildHoneypotConfig,
        before: datetime,
    ) -> HoneypotScanResult:
        """Scan one config while enforcing one active scan per guild."""

        lock = self._scan_locks[config.guild_id]
        if lock.locked():
            raise HoneypotScanAlreadyRunningError(config.guild_id)

        async with lock:
            return await self._scan_config(config=config, before=before)

    async def scan_guild(
        self,
        guild_id: int,
        *,
        ignore_disabled: bool = False,
    ) -> HoneypotScanResult:
        """Scan the configured honeypot channel for one guild."""

        config = self.get_config(guild_id)
        if config is None:
            return HoneypotScanResult()
        if not config.enabled and not ignore_disabled:
            return HoneypotScanResult(configs_checked=1)

        log.info(f'Starting honeypot scan for guild {guild_id}.')
        result = await self._scan_config_with_lock(
            config=config,
            before=discord.utils.utcnow(),
        )
        log.info(f'Finished honeypot scan for guild {guild_id}.')
        return result

    async def _scan_enabled_config(
        self,
        *,
        config: GuildHoneypotConfig,
        before: datetime,
    ) -> HoneypotScanResult:
        """Scan one enabled config for a multi-guild scan."""

        try:
            return await self._scan_config_with_lock(
                config=config,
                before=before,
            )
        except HoneypotScanAlreadyRunningError:
            log.warning(
                f'Skipping honeypot scan for guild {config.guild_id}; '
                f'a scan is already running.'
            )
            return HoneypotScanResult()
        except Exception:
            log.exception(f'Honeypot scan failed for guild {config.guild_id}.')
            return HoneypotScanResult(configs_checked=1)

    async def scan_enabled_configs(self) -> HoneypotScanResult:
        """Scan all enabled honeypot configs."""

        result = HoneypotScanResult()
        configs = self.enabled_configs()
        if not configs:
            return result

        scan_started_at = discord.utils.utcnow()
        log.info(f'Starting honeypot scan for {len(configs)} configs.')

        # Scan each config scan concurrently
        async with asyncio.TaskGroup() as task_group:
            tasks = [
                task_group.create_task(
                    self._scan_enabled_config(
                        config=config,
                        before=scan_started_at,
                    )
                )
                for config in configs
            ]

        for task in tasks:
            result.merge(task.result())

        log.info('Finished honeypot scan.')
        return result

    def start_scan_once(self) -> None:
        """Start the one-time automatic honeypot scan."""

        if self._scan_once_done:
            return

        def handle_scan_once_done(task: asyncio.Task[Any]) -> None:
            """Log automatic scan task failures."""
            try:
                task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception('Honeypot scan failed.')

        self._scan_once_done = True
        self._scan_once_task = asyncio.create_task(self.scan_enabled_configs())
        self._scan_once_task.add_done_callback(handle_scan_once_done)

    def mark_scan_once_done(self) -> None:
        """Mark the automatic scan as already handled for this process."""
        self._scan_once_done = True

    def cancel_scan(self) -> None:
        """Cancel a pending automatic scan."""
        if self._scan_once_task is not None and not self._scan_once_task.done():
            self._scan_once_task.cancel()

        if self._rebuild_bloom_task is not None and not self._rebuild_bloom_task.done():
            self._rebuild_bloom_task.cancel()

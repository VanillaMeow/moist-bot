from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING, cast

import discord
from sqlalchemy import update
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


log = logging.getLogger('discord.' + __name__)

CONTENT_EXCERPT_WIDTH = 500
PUNISHMENT_REASON = 'Triggered honeypot channel.'
SOFTBAN_DELETE_MESSAGE_SECONDS = 5 * 60
DISCORD_MAX_DELETE_MESSAGE_SECONDS = 7 * 24 * 60 * 60
SCAN_DELETE_SECONDS_GRACE = 5
BAN_TRIGGER_MODULO = 3


class HoneypotPunishmentAction(StrEnum):
    SOFTBAN = auto()
    BAN = auto()


@dataclass(frozen=True, slots=True)
class HoneypotConfig:
    """Cached honeypot configuration for a guild."""

    id: int
    guild_id: int
    channel_id: int
    log_channel_id: int
    alert_message_id: int | None
    enabled: bool


@dataclass(slots=True)
class HoneypotScanBatch:
    """Messages found for one member during a honeypot scan."""

    member: discord.Member
    messages: list[discord.Message]


@dataclass(frozen=True, slots=True)
class HoneypotPunishmentResult:
    """Outcome of an automatic honeypot punishment."""

    action: HoneypotPunishmentAction
    trigger_count: int
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
        self.configs: dict[int, HoneypotConfig] = {}
        self._incident_counts: defaultdict[int, int] = defaultdict(int)
        self._load_lock: asyncio.Lock = asyncio.Lock()
        self._scan_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._scan_once_done: bool = False
        self._scan_once_task: asyncio.Task[Any] | None = None

    async def load(self) -> None:
        """Load all honeypot configs into memory."""

        async with self._load_lock:
            async with self.bot.db_session_maker() as session:
                result = await session.execute(select(GuildHoneypotConfig))
                rows = list(result.scalars().all())
                incident_counts = await HoneypotGuildStats.counts_by_guild(session)

            self.configs.clear()
            self._incident_counts = defaultdict(int, incident_counts)
            for row in rows:
                self._cache_config(row)

            log.info(f'Loaded {len(rows)} honeypot configs.')

    def _cache_config(self, config: GuildHoneypotConfig) -> HoneypotConfig:
        """Store a database config row in the hot-path cache."""

        if config.id is None:
            msg = 'Cannot cache a honeypot config before it has an ID.'
            raise ValueError(msg)

        cached = HoneypotConfig(
            id=config.id,
            guild_id=config.guild_id,
            channel_id=config.channel_id,
            log_channel_id=config.log_channel_id,
            alert_message_id=config.alert_message_id,
            enabled=config.enabled,
        )
        self.configs[config.guild_id] = cached
        return cached

    def get_config(self, guild_id: int) -> HoneypotConfig | None:
        """Return the cached config for a guild."""
        return self.configs.get(guild_id)

    def enabled_configs(self) -> tuple[HoneypotConfig, ...]:
        """Return all enabled cached honeypot configs."""
        return tuple(config for config in self.configs.values() if config.enabled)

    async def _is_exempt(self, message: discord.Message) -> bool:
        """Return whether a message should bypass automatic honeypot action."""

        author = message.author
        return (
            message.guild is None
            or message.author.bot
            or message.webhook_id is not None
            or await self.bot.is_owner(author)
            or not isinstance(author, discord.Member)
            or author.guild_permissions.manage_guild
            or author.guild_permissions.administrator
        )

    @staticmethod
    def _content_excerpt(message: discord.Message) -> str | None:
        """Return a bounded content excerpt for logs and incident storage."""

        content = message.content.strip()
        if not content:
            return None
        return shorten(content, CONTENT_EXCERPT_WIDTH)

    @staticmethod
    def _delete_seconds_for_oldest_message(message: discord.Message) -> int:
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

    async def set_config(
        self,
        *,
        guild_id: int,
        channel_id: int,
        log_channel_id: int,
        updated_by_id: int,
    ) -> HoneypotConfig:
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
            cached = self._cache_config(config)
            await session.commit()

        return cached

    async def set_alert_message_id(
        self,
        *,
        guild_id: int,
        alert_message_id: int,
        updated_by_id: int,
    ) -> HoneypotConfig | None:
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
            cached = self._cache_config(config)
            await session.commit()

        self.configs[guild_id] = cached
        return cached

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
            cached = self._cache_config(config)
            await session.commit()

        self.configs[guild_id] = cached
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

    async def _reserve_trigger_count(
        self,
        *,
        guild_id: int,
        user_id: int,
    ) -> int:
        """Increment and return the member trigger count used for punishment."""

        async with self.bot.db_session_maker() as session:
            trigger_count = await HoneypotUserStats.increment(
                session,
                guild_id=guild_id,
                user_id=user_id,
            )
            await session.commit()

        return trigger_count

    async def _create_incident(
        self,
        incident: HoneypotIncident,
    ) -> HoneypotIncident:
        """Create one incident row and update precomputed guild stats."""

        async with self.bot.db_session_maker() as session:
            await HoneypotGuildStats.increment(session, guild_id=incident.guild_id)
            session.add(incident)
            await session.commit()

        self._increment_incident_count(incident.guild_id)
        return incident

    async def update_log_status(
        self,
        *,
        incident_id: int,
        log_sent: bool,
        log_error: str | None,
    ) -> None:
        """Record whether the incident log embed was delivered."""

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(HoneypotIncident).where(col(HoneypotIncident.id) == incident_id)
            )
            incident = result.scalar_one_or_none()
            if incident is None:
                return

            incident.log_sent = log_sent
            incident.log_error = log_error
            await session.commit()

    async def _send_log_embed(
        self,
        *,
        incident: HoneypotIncident,
        message: discord.Message,
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
            f'in guild {message.guild} '
            f'({message.guild.id if message.guild is not None else None}), '
            f'message {message.id}.'
        )
        return True, None

    async def _record_and_log_trigger(
        self,
        *,
        message: discord.Message,
        member: discord.Member,
        config: HoneypotConfig,
        delete_message_seconds: int,
        punishment: HoneypotPunishmentResult,
    ) -> None:
        """Create the incident row and send the configured log."""

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
            delete_message_seconds=delete_message_seconds,
            punishment_action=str(punishment.action),
            punishment_succeeded=punishment.succeeded,
            punishment_error=punishment.error,
        )
        await self._create_incident(incident)

        log_sent, log_error = await self._send_log_embed(
            incident=incident,
            message=message,
            member=member,
        )
        if incident.id is not None:
            await self.update_log_status(
                incident_id=incident.id,
                log_sent=log_sent,
                log_error=log_error,
            )

    async def punish_member(
        self,
        member: discord.Member,
        *,
        trigger_count: int,
        delete_message_seconds: int,
    ) -> HoneypotPunishmentResult:
        """Punish a member according to their honeypot trigger history."""

        action = (
            HoneypotPunishmentAction.BAN
            if trigger_count % BAN_TRIGGER_MODULO == 0
            else HoneypotPunishmentAction.SOFTBAN
        )

        succeeded: bool = True
        error: str | None = None
        ban_applied: bool = True

        match action:
            case HoneypotPunishmentAction.BAN:
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

            case HoneypotPunishmentAction.SOFTBAN:
                result = await self.bot.softban_member(
                    member,
                    reason=PUNISHMENT_REASON,
                    delete_message_seconds=delete_message_seconds,
                )
                succeeded = result.softbanned
                error = (
                    shorten(result.error, CONTENT_EXCERPT_WIDTH)
                    if result.error
                    else None
                )
                ban_applied = result.ban_applied

        # Finally, record the punishment
        return HoneypotPunishmentResult(
            action=action,
            trigger_count=trigger_count,
            succeeded=succeeded,
            error=error,
            ban_applied=ban_applied,
        )

    async def _handle_trigger(
        self,
        *,
        message: discord.Message,
        member: discord.Member,
        config: HoneypotConfig,
        delete_message_seconds: int = SOFTBAN_DELETE_MESSAGE_SECONDS,
    ) -> None:
        """Run the full punishment flow for a honeypot trigger."""

        trigger_count: int = await self._reserve_trigger_count(
            guild_id=config.guild_id,
            user_id=member.id,
        )
        punishment: HoneypotPunishmentResult = await self.punish_member(
            member,
            trigger_count=trigger_count,
            delete_message_seconds=delete_message_seconds,
        )

        await self._record_and_log_trigger(
            message=message,
            member=member,
            config=config,
            delete_message_seconds=delete_message_seconds,
            punishment=punishment,
        )

    async def handle_message(self, message: discord.Message) -> None:
        """Handle a live message sent to a configured honeypot channel."""

        # Early reject conditions
        if await self._is_exempt(message):
            return

        # These are already confirmed by `self._is_exempt`
        if TYPE_CHECKING:
            assert message.guild is not None
            assert isinstance(message.author, discord.Member)

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
            member=message.author,
            config=config,
        )

    async def _resolve_scan_channel(
        self,
        config: HoneypotConfig,
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
                assert message.guild is not None
                assert isinstance(message.author, discord.Member)

            self._add_scan_message(
                batches,
                member=message.author,
                message=message,
            )

        return batches

    @staticmethod
    def _add_scan_message(
        batches: dict[int, HoneypotScanBatch],
        *,
        member: discord.Member,
        message: discord.Message,
    ) -> None:
        """Add one scanned message to a member batch."""

        batch = batches.get(member.id)
        if batch is None:
            batch = HoneypotScanBatch(member=member, messages=[])
            batches[member.id] = batch
        batch.messages.append(message)

    async def _delete_scan_messages(
        self,
        *,
        messages: list[discord.Message],
    ) -> int:
        """Deletes scanned honeypot messages not covered by ban deletion."""

        if not messages:
            return 0

        channel = cast('discord.abc.Messageable', messages[0].channel)
        purger = ChannelPurger(channel)
        deleted = await purger.delete_messages(messages)
        failed_count = len(messages) - len(deleted)
        if failed_count:
            log.warning(f'Failed to manually delete {failed_count} honeypot messages.')
        return len(deleted)

    @staticmethod
    def _scan_messages_requiring_manual_delete(
        *,
        messages: list[discord.Message],
        delete_message_seconds: int,
        ban_applied: bool,
    ) -> list[discord.Message]:
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
        config: HoneypotConfig,
        batch: HoneypotScanBatch,
    ) -> int:
        """Handle all scanned honeypot messages for one member."""

        oldest_message = min(batch.messages, key=lambda message: message.created_at)
        delete_message_seconds = self._delete_seconds_for_oldest_message(oldest_message)
        trigger_count = await self._reserve_trigger_count(
            guild_id=config.guild_id,
            user_id=batch.member.id,
        )
        punishment = await self.punish_member(
            batch.member,
            trigger_count=trigger_count,
            delete_message_seconds=delete_message_seconds,
        )
        manual_delete_messages = self._scan_messages_requiring_manual_delete(
            messages=batch.messages,
            delete_message_seconds=delete_message_seconds,
            ban_applied=punishment.ban_applied,
        )
        ban_deleted_count = len(batch.messages) - len(manual_delete_messages)
        manual_deleted_count = await self._delete_scan_messages(
            messages=manual_delete_messages
        )
        deleted_count = ban_deleted_count + manual_deleted_count
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

        await self._record_and_log_trigger(
            message=oldest_message,
            member=batch.member,
            config=config,
            delete_message_seconds=delete_message_seconds,
            punishment=punishment,
        )
        return deleted_count

    async def _scan_config(
        self,
        *,
        config: HoneypotConfig,
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
            result.messages_deleted += await self._handle_scan_batch(
                config=config,
                batch=batch,
            )
            result.members_handled += 1
            result.incidents_recorded += 1

        return result

    async def _scan_config_with_lock(
        self,
        *,
        config: HoneypotConfig,
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
        config: HoneypotConfig,
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

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, cast

import discord

from moist_bot.utils.message_purge import ChannelPurger

from .constants import (
    DISCORD_MAX_DELETE_MESSAGE_SECONDS,
    SCAN_DELETE_SECONDS_GRACE,
)
from .types import (
    GuildMessage,
    HoneypotScanAlreadyRunningError,
    HoneypotScanBatch,
    HoneypotScanBatchResult,
    HoneypotScanResult,
)

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any

    from moist_bot.models import GuildHoneypotConfig

    from .manager import HoneypotManager


log = logging.getLogger('discord.' + __name__)


class HoneypotScanner:
    """Scan configured honeypot channels for existing messages."""

    def __init__(self, manager: HoneypotManager) -> None:
        self.manager: HoneypotManager = manager
        self._scan_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._scan_once_done: bool = False
        self._scan_once_task: asyncio.Task[Any] | None = None

    @staticmethod
    def delete_seconds_for_oldest_message(message: GuildMessage) -> int:
        """Return a Discord delete window for a scanned honeypot message."""

        age = discord.utils.utcnow() - message.created_at
        seconds = int(max(0.0, age.total_seconds())) + SCAN_DELETE_SECONDS_GRACE
        return min(seconds, DISCORD_MAX_DELETE_MESSAGE_SECONDS)

    async def _resolve_scan_channel(
        self,
        config: GuildHoneypotConfig,
    ) -> discord.TextChannel | None:
        """Resolve a configured honeypot scan channel."""

        try:
            guild = await self.manager.bot.get_or_fetch_guild(config.guild_id)
        except discord.HTTPException:
            log.warning(f'Skipping honeypot scan for missing guild {config.guild_id}.')
            return None

        try:
            channel = await self.manager.bot.get_or_fetch_channel(
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
            if await self.manager.is_exempt(message):
                continue

            # These are already confirmed by `self.manager.is_exempt`
            if TYPE_CHECKING:
                message = cast('GuildMessage', message)

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

        recorded_ids = await self.manager.recorded_message_ids(
            config.guild_id,
            [message.id for message in batch.messages],
        )

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
            unrecorded_messages, key=lambda message: message.created_at
        )
        delete_message_seconds = self.delete_seconds_for_oldest_message(oldest_message)
        trigger_count = self.manager.next_trigger_count(
            config.guild_id, batch.member.id
        )
        punishment = await self.manager.punish_member(
            batch.member,
            trigger_count=trigger_count,
            delete_message_seconds=delete_message_seconds,
        )
        incident_recorded = await self.manager.log_and_record_trigger(
            oldest_message,
            batch.member,
            config,
            punishment,
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
        self, *, config: GuildHoneypotConfig, before: datetime
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
        self, *, config: GuildHoneypotConfig, before: datetime
    ) -> HoneypotScanResult:
        """Scan one config while enforcing one active scan per guild."""

        lock = self._scan_locks[config.guild_id]
        if lock.locked():
            raise HoneypotScanAlreadyRunningError(config.guild_id)

        async with lock:
            return await self._scan_config(config=config, before=before)

    async def scan(
        self, guild_id: int, *, ignore_disabled: bool = False
    ) -> HoneypotScanResult:
        """Scan the configured honeypot channel for one guild."""

        config = self.manager.config.get(guild_id)
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
        self, *, config: GuildHoneypotConfig, before: datetime
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

    async def scan_enabled(self) -> HoneypotScanResult:
        """Scan all enabled honeypot configs."""

        result = HoneypotScanResult()
        configs = self.manager.config.enabled()
        if not configs:
            return result

        scan_started_at = discord.utils.utcnow()
        log.info(f'Starting honeypot scan for {len(configs)} configs.')

        async with asyncio.TaskGroup() as task_group:
            tasks = [
                task_group.create_task(
                    self._scan_enabled_config(config=config, before=scan_started_at)
                )
                for config in configs
            ]

        for task in tasks:
            result.merge(task.result())

        log.info('Finished honeypot scan.')
        return result

    def start_once(self) -> None:
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
        self._scan_once_task = asyncio.create_task(self.scan_enabled())
        self._scan_once_task.add_done_callback(handle_scan_once_done)

    def mark_once_done(self) -> None:
        """Mark the automatic scan as already handled for this process."""
        self._scan_once_done = True

    def cancel(self) -> None:
        """Cancel a pending automatic scan."""
        if self._scan_once_task is not None and not self._scan_once_task.done():
            self._scan_once_task.cancel()

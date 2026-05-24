from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import discord
from sqlalchemy import update
from sqlmodel import col, select

from moist_bot.models import GuildHoneypotConfig, HoneypotIncident
from moist_bot.utils.converters import shorten
from moist_bot.utils.formats import plural

if TYPE_CHECKING:
    from datetime import datetime

    from moist_bot.bot import MoistBot


log = logging.getLogger('discord.' + __name__)

CONTENT_EXCERPT_WIDTH = 500
SOFTBAN_REASON = 'Triggered honeypot channel.'
SOFTBAN_DELETE_MESSAGE_SECONDS = 5 * 60
DISCORD_MAX_DELETE_MESSAGE_SECONDS = 7 * 24 * 60 * 60
STARTUP_SCAN_DELETE_SECONDS_GRACE = 5


@dataclass(frozen=True, slots=True)
class HoneypotConfig:
    """Cached honeypot configuration for a guild."""

    id: int
    guild_id: int
    channel_id: int
    log_channel_id: int
    enabled: bool


@dataclass(slots=True)
class StartupHoneypotBatch:
    """Messages found for one member during startup scanning."""

    member: discord.Member
    messages: list[discord.Message]


class HoneypotManager:
    """Manage persistent honeypot config and incident records."""

    def __init__(self, bot: MoistBot) -> None:
        self.bot: MoistBot = bot
        self.configs: dict[int, HoneypotConfig] = {}
        self._load_lock: asyncio.Lock = asyncio.Lock()
        self._startup_scan_done: bool = False
        self._startup_scan_task: asyncio.Task[None] | None = None

    async def load(self) -> None:
        """Load all honeypot configs into memory."""

        async with self._load_lock:
            async with self.bot.db_session_maker() as session:
                result = await session.execute(select(GuildHoneypotConfig))
                rows = list(result.scalars().all())

            self.configs.clear()
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

    async def _is_exempt(self, member: discord.Member) -> bool:
        """Return whether a member should bypass automatic honeypot action."""

        return (
            await self.bot.is_owner(member)
            or member.guild_permissions.administrator
            or member.guild_permissions.manage_guild
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
        """Return a Discord delete window for a scanned startup message."""

        age = discord.utils.utcnow() - message.created_at
        seconds = int(max(0.0, age.total_seconds())) + STARTUP_SCAN_DELETE_SECONDS_GRACE
        return min(seconds, DISCORD_MAX_DELETE_MESSAGE_SECONDS)

    async def incident_count_for_guild(self, *, guild_id: int) -> int:
        """Return the total number of honeypot incidents for a guild."""

        async with self.bot.db_session_maker() as session:
            return await HoneypotIncident.history_count(
                session,
                criteria=(col(HoneypotIncident.guild_id) == guild_id,),
            )

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

            config.channel_id = channel_id
            config.log_channel_id = log_channel_id
            config.enabled = True
            config.updated_at = discord.utils.utcnow()
            config.updated_by_id = updated_by_id
            await session.flush()
            cached = self._cache_config(config)
            await session.commit()

        return cached

    async def disable_config(self, *, guild_id: int, updated_by_id: int) -> bool:
        """Disable a guild honeypot config if one exists."""

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildHoneypotConfig).where(
                    col(GuildHoneypotConfig.guild_id) == guild_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                return False

            config.enabled = False
            config.updated_at = discord.utils.utcnow()
            config.updated_by_id = updated_by_id
            await session.flush()
            cached = self._cache_config(config)
            await session.commit()

        self.configs[guild_id] = cached
        return True

    async def delete_config(self, *, guild_id: int) -> bool:
        """Delete a guild honeypot config while preserving incident history."""

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

    async def create_incident(
        self,
        *,
        config: HoneypotConfig,
        user_id: int,
        message_id: int,
        message_created_at: datetime,
        content_excerpt: str | None,
        attachment_count: int,
        delete_message_seconds: int,
        softbanned: bool,
        softban_error: str | None,
    ) -> HoneypotIncident:
        """Create one incident row and return it with the updated trigger count."""

        async with self.bot.db_session_maker() as session:
            trigger_count = (
                await HoneypotIncident.trigger_count_for_user(
                    session,
                    guild_id=config.guild_id,
                    user_id=user_id,
                )
            ) + 1
            incident = HoneypotIncident(
                config_id=config.id,
                guild_id=config.guild_id,
                channel_id=config.channel_id,
                log_channel_id=config.log_channel_id,
                user_id=user_id,
                message_id=message_id,
                message_created_at=message_created_at,
                content_excerpt=content_excerpt,
                attachment_count=attachment_count,
                trigger_count=trigger_count,
                delete_message_seconds=delete_message_seconds,
                softbanned=softbanned,
                softban_error=softban_error,
            )
            session.add(incident)
            await session.commit()

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

    async def _softban_member(
        self,
        member: discord.Member,
        *,
        delete_message_seconds: int,
    ) -> tuple[bool, str | None]:
        """Ban and unban a member to remove recent messages."""

        try:
            await member.ban(
                reason=SOFTBAN_REASON,
                delete_message_seconds=delete_message_seconds,
            )
        except discord.HTTPException as e:
            log.warning(
                f'Failed to ban honeypot user {member} ({member.id}) '
                f'in guild {member.guild} ({member.guild.id}): {e}'
            )
            return False, shorten(f'Ban failed: {e}', CONTENT_EXCERPT_WIDTH)

        try:
            await member.unban(reason=SOFTBAN_REASON)
        except discord.HTTPException as e:
            log.warning(
                f'Failed to unban honeypot user {member} ({member.id}) '
                f'in guild {member.guild} ({member.guild.id}): {e}'
            )
            return False, shorten(f'Unban failed: {e}', CONTENT_EXCERPT_WIDTH)

        return True, None

    async def _send_log_embed(
        self,
        *,
        incident: HoneypotIncident,
        message: discord.Message,
        member: discord.Member,
    ) -> tuple[bool, str | None]:
        """Send the configured incident log embed."""

        channel = self.bot.get_channel(incident.log_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(incident.log_channel_id)
            except discord.HTTPException as e:
                return False, shorten(str(e), CONTENT_EXCERPT_WIDTH)

        if not hasattr(channel, 'send'):
            return False, 'Configured log channel cannot receive messages.'
        channel = cast('discord.abc.Messageable', channel)

        embed = (
            discord.Embed(
                title='\N{HONEY POT} Honeypot Triggered',
                colour=discord.Colour.red(),
                timestamp=incident.triggered_at,
                description=incident.content_excerpt,
            )
            .set_author(name=f'{member.name} ({member.id})', icon_url=member.display_avatar.url)
            .set_footer(text=f'{plural(incident.trigger_count):trigger} from user')
        )

        if incident.attachment_count > 0:
            embed.add_field(
                name='Attachments',
                value=str(incident.attachment_count),
            )

        if incident.softban_error is not None:
            embed.add_field(
                name='Softban Error',
                value=incident.softban_error,
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
        softbanned: bool,
        softban_error: str | None,
    ) -> None:
        """Create the incident row and send the configured log."""

        incident = await self.create_incident(
            config=config,
            user_id=member.id,
            message_id=message.id,
            message_created_at=message.created_at,
            content_excerpt=self._content_excerpt(message),
            attachment_count=len(message.attachments),
            delete_message_seconds=delete_message_seconds,
            softbanned=softbanned,
            softban_error=softban_error,
        )

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

    async def _handle_trigger(
        self,
        *,
        message: discord.Message,
        member: discord.Member,
        config: HoneypotConfig,
        delete_message_seconds: int = SOFTBAN_DELETE_MESSAGE_SECONDS,
    ) -> None:
        """Run the full softban flow for a honeypot trigger."""

        # Ban deletion is safer than manual purge because Discord handles scope
        softbanned, softban_error = await self._softban_member(
            member,
            delete_message_seconds=delete_message_seconds,
        )

        # Store the incident before logging so the embed can show trigger count
        await self._record_and_log_trigger(
            message=message,
            member=member,
            config=config,
            delete_message_seconds=delete_message_seconds,
            softbanned=softbanned,
            softban_error=softban_error,
        )

    async def handle_message(self, message: discord.Message) -> None:
        """Handle a live message sent to a configured honeypot channel."""

        # Early reject conditions
        if (
            message.guild is None
            or message.author.bot
            or message.webhook_id is not None
        ):
            return

        # Config reject conditions
        config = self.get_config(message.guild.id)
        if (
            config is None
            or message.channel.id != config.channel_id
            or not config.enabled
            or not isinstance(message.author, discord.Member)
            or await self._is_exempt(message.author)
        ):
            return

        # Only non-exempt human members reaching this point trigger the softban
        await self._handle_trigger(
            message=message,
            member=message.author,
            config=config,
        )

    async def _resolve_startup_channel(
        self,
        config: HoneypotConfig,
    ) -> discord.TextChannel | None:
        """Resolve a configured startup scan channel."""

        guild = self.bot.get_guild(config.guild_id)
        if guild is None:
            log.warning(
                f'Skipping honeypot startup scan for missing guild {config.guild_id}.'
            )
            return None

        channel = guild.get_channel(config.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(config.channel_id)
            except discord.HTTPException as e:
                log.warning(
                    f'Failed to fetch honeypot channel {config.channel_id} '
                    f'in guild {guild} ({guild.id}): {e}'
                )
                return None

        if not isinstance(channel, discord.TextChannel):
            log.warning(
                f'Skipping honeypot startup scan for non-text channel '
                f'{config.channel_id} in guild {guild} ({guild.id}).'
            )
            return None

        permissions = channel.permissions_for(guild.me)
        if not permissions.read_messages or not permissions.read_message_history:
            log.warning(
                f'Skipping honeypot startup scan for {channel} ({channel.id}) '
                f'in guild {guild} ({guild.id}); missing history permissions.'
            )
            return None

        return channel

    async def _member_for_startup_message(
        self,
        guild: discord.Guild,
        message: discord.Message,
    ) -> discord.Member | None:
        """Return the non-exempt member that authored a scanned message."""

        if message.webhook_id is not None or message.author.bot:
            return None

        if isinstance(message.author, discord.Member):
            member = message.author
        else:
            try:
                member = await guild.fetch_member(message.author.id)
            except discord.HTTPException:
                return None

        if await self._is_exempt(member):
            return None
        return member

    async def _scan_startup_channel(
        self,
        *,
        channel: discord.TextChannel,
        before: datetime,
    ) -> dict[int, StartupHoneypotBatch]:
        """Scan a honeypot channel and group missed messages by member."""

        batches: dict[int, StartupHoneypotBatch] = {}
        try:
            async for message in channel.history(limit=None, before=before):
                member = await self._member_for_startup_message(channel.guild, message)
                if member is not None:
                    self._add_startup_message(
                        batches,
                        member=member,
                        message=message,
                    )
        except discord.HTTPException as e:
            log.warning(
                f'Failed to scan honeypot channel {channel} ({channel.id}) '
                f'in guild {channel.guild} ({channel.guild.id}): {e}'
            )

        return batches

    @staticmethod
    def _add_startup_message(
        batches: dict[int, StartupHoneypotBatch],
        *,
        member: discord.Member,
        message: discord.Message,
    ) -> None:
        """Add one scanned message to a member batch."""

        batch = batches.get(member.id)
        if batch is None:
            batch = StartupHoneypotBatch(member=member, messages=[])
            batches[member.id] = batch
        batch.messages.append(message)

    async def _delete_startup_messages(
        self,
        *,
        messages: list[discord.Message],
    ) -> int:
        """Deletes scanned honeypot messages after a startup softban."""

        deleted_count = 0
        failed_count = 0
        for message in messages:
            try:
                await message.delete()
            except discord.NotFound:
                continue
            except discord.HTTPException:
                failed_count += 1
            else:
                deleted_count += 1

        if failed_count:
            log.warning(f'Failed to manually delete {failed_count} honeypot messages.')
        return deleted_count

    async def _handle_startup_batch(
        self,
        *,
        config: HoneypotConfig,
        batch: StartupHoneypotBatch,
    ) -> None:
        """Handle all scanned startup messages for one member."""

        oldest_message = min(batch.messages, key=lambda message: message.created_at)
        delete_message_seconds = self._delete_seconds_for_oldest_message(oldest_message)
        softbanned, softban_error = await self._softban_member(
            batch.member,
            delete_message_seconds=delete_message_seconds,
        )
        deleted_count = await self._delete_startup_messages(messages=batch.messages)
        if deleted_count:
            log.debug(
                f'Deleted {deleted_count} scanned honeypot messages for '
                f'{batch.member} ({batch.member.id}).'
            )

        await self._record_and_log_trigger(
            message=oldest_message,
            member=batch.member,
            config=config,
            delete_message_seconds=delete_message_seconds,
            softbanned=softbanned,
            softban_error=softban_error,
        )

    async def run_startup_scan(self) -> None:
        """Scan enabled honeypot channels for messages missed while offline."""

        scan_started_at = discord.utils.utcnow()
        configs = self.enabled_configs()
        if not configs:
            return

        log.info(f'Starting honeypot startup scan for {len(configs)} configs.')
        for config in configs:
            channel = await self._resolve_startup_channel(config)
            if channel is None:
                continue

            batches = await self._scan_startup_channel(
                channel=channel,
                before=scan_started_at,
            )
            for batch in batches.values():
                await self._handle_startup_batch(config=config, batch=batch)

        log.info('Finished honeypot startup scan.')

    def handle_startup_scan_done(self, task: asyncio.Task[None]) -> None:
        """Log startup scan task failures."""

        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception('Honeypot startup scan failed.')

    def start_startup_scan(self) -> None:
        """Start the one-time honeypot startup scan."""

        if self._startup_scan_done:
            return

        self._startup_scan_done = True
        self._startup_scan_task = asyncio.create_task(self.run_startup_scan())
        self._startup_scan_task.add_done_callback(self.handle_startup_scan_done)

    def mark_startup_scan_done(self) -> None:
        """Mark the startup scan as already handled for this process."""

        self._startup_scan_done = True

    def cancel_startup_scan(self) -> None:
        """Cancel a pending startup scan."""

        if self._startup_scan_task is not None and not self._startup_scan_task.done():
            self._startup_scan_task.cancel()

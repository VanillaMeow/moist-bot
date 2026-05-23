from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from sqlmodel import col, select

from moist_bot.models import GuildHoneypotConfig, HoneypotIncident

if TYPE_CHECKING:
    from datetime import datetime

    from moist_bot.bot import MoistBot


log = logging.getLogger('discord.' + __name__)


@dataclass(frozen=True, slots=True)
class HoneypotConfig:
    """Cached honeypot configuration for a guild."""

    id: int
    guild_id: int
    channel_id: int
    log_channel_id: int
    enabled: bool


class HoneypotManager:
    """Manage persistent honeypot config and incident records."""

    def __init__(self, bot: MoistBot) -> None:
        self.bot: MoistBot = bot
        self.configs: dict[int, HoneypotConfig] = {}

    async def load(self) -> None:
        """Load all honeypot configs into memory."""

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

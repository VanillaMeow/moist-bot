from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from sqlalchemy import select as sa_select, update
from sqlmodel import col, select

from moist_bot.db.constants import DATABASE_STREAM_BATCH_SIZE
from moist_bot.models import GuildHoneypotConfig, HoneypotIncident

if TYPE_CHECKING:
    from .manager import HoneypotManager


log = logging.getLogger('discord.' + __name__)


class HoneypotConfig:
    """Manage persistent guild honeypot configuration."""

    def __init__(self, manager: HoneypotManager) -> None:
        self.manager: HoneypotManager = manager
        self._cache: dict[int, GuildHoneypotConfig] = {}
        self._load_lock: asyncio.Lock = asyncio.Lock()

    async def load(self) -> None:
        """Load all honeypot configs into memory."""

        async with self._load_lock:
            cache: dict[int, GuildHoneypotConfig] = {}
            async with self.manager.bot.db_session_maker() as session:
                statement = sa_select(GuildHoneypotConfig).execution_options(
                    yield_per=DATABASE_STREAM_BATCH_SIZE
                )
                result = await session.stream_scalars(statement)
                async for config in result:
                    cache[config.guild_id] = config

            self._cache = cache
            log.info(f'Loaded {len(cache)} honeypot configs.')

    def get(self, guild_id: int) -> GuildHoneypotConfig | None:
        """Return the cached config for a guild."""
        return self._cache.get(guild_id)

    def enabled(self) -> tuple[GuildHoneypotConfig, ...]:
        """Return all enabled cached honeypot configs."""
        return tuple(config for config in self._cache.values() if config.enabled)

    async def set(
        self,
        *,
        guild_id: int,
        channel_id: int,
        log_channel_id: int,
        updated_by_id: int,
    ) -> GuildHoneypotConfig:
        """Create or update a guild honeypot config."""

        async with self.manager.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildHoneypotConfig).where(
                    GuildHoneypotConfig.guild_id == guild_id
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

        self._cache[guild_id] = config
        return config

    async def set_alert_message_id(
        self,
        *,
        guild_id: int,
        alert_message_id: int,
        updated_by_id: int,
    ) -> GuildHoneypotConfig | None:
        """Update a guild config's alert message ID."""

        async with self.manager.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildHoneypotConfig).where(
                    GuildHoneypotConfig.guild_id == guild_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                self._cache.pop(guild_id, None)
                return None

            config.alert_message_id = alert_message_id
            config.updated_at = discord.utils.utcnow()
            config.updated_by_id = updated_by_id
            await session.flush()
            await session.commit()

        self._cache[guild_id] = config
        return config

    async def disable(self, *, guild_id: int, updated_by_id: int) -> bool:
        """Disable a guild honeypot config if one exists."""

        return await self.set_enabled(
            guild_id=guild_id,
            enabled=False,
            updated_by_id=updated_by_id,
        )

    async def enable(self, *, guild_id: int, updated_by_id: int) -> bool:
        """Enable a guild honeypot config if one exists."""

        return await self.set_enabled(
            guild_id=guild_id,
            enabled=True,
            updated_by_id=updated_by_id,
        )

    async def set_enabled(
        self,
        *,
        guild_id: int,
        enabled: bool,
        updated_by_id: int,
    ) -> bool:
        """Set whether a guild honeypot config is enabled if one exists."""

        async with self.manager.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildHoneypotConfig).where(
                    GuildHoneypotConfig.guild_id == guild_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                self._cache.pop(guild_id, None)
                return False

            config.enabled = enabled
            config.updated_at = discord.utils.utcnow()
            config.updated_by_id = updated_by_id
            await session.flush()
            await session.commit()

        self._cache[guild_id] = config
        return True

    async def delete(self, *, guild_id: int) -> bool:
        """Delete a guild config while preserving incident history."""

        async with self.manager.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildHoneypotConfig).where(
                    GuildHoneypotConfig.guild_id == guild_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                self._cache.pop(guild_id, None)
                return False

            await session.execute(
                update(HoneypotIncident)
                .where(col(HoneypotIncident.guild_id) == guild_id)
                .values(config_id=None)
            )
            await session.delete(config)
            await session.commit()

        self._cache.pop(guild_id, None)
        return True

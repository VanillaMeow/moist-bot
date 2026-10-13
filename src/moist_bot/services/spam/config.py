"""Persist guild configuration and publish immutable snapshots after commit."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy import delete
from sqlmodel import col, select

from moist_bot.models.spam import GuildSpamConfig, GuildSpamExemption

from .actions import validate_permissions
from .types import (
    ExemptionKind,
    GuildSpamSettings,
    RateSettings,
    SpamRule,
    SpamSettings,
)

if TYPE_CHECKING:
    import discord

    from .manager import SpamManager


class SpamConfig:
    """Cache guild settings and serialize changes with the shared counter lock."""

    def __init__(self, manager: SpamManager) -> None:
        """Keep the owning manager and a disabled default for unconfigured guilds."""

        self.manager = manager
        self._cache: dict[int, GuildSpamSettings] = {}
        self._default = GuildSpamSettings(enabled=False, settings=SpamSettings())

    async def load(self) -> None:
        """Load settings and exemptions into a fresh startup cache."""

        async with self.manager.bot.db_session_maker() as session:
            configs = (await session.execute(select(GuildSpamConfig))).scalars().all()
            exemptions = (
                (await session.execute(select(GuildSpamExemption))).scalars().all()
            )

        # Group exemptions once rather than scanning every row for each guild
        entities: dict[int, set[tuple[ExemptionKind, int]]] = {}
        for row in exemptions:
            entities.setdefault(row.guild_id, set()).add(
                (ExemptionKind(row.kind), row.entity_id)
            )

        self._cache = {
            config.guild_id: GuildSpamSettings(
                enabled=config.enabled,
                settings=SpamSettings.model_validate_json(config.settings_json),
                exemptions=frozenset(entities.get(config.guild_id, ())),
            )
            for config in configs
        }

    def get(self, guild_id: int) -> GuildSpamSettings:
        """Read cached configuration without querying the database.

        Parameters
        ----------
        guild_id : int
            Guild whose current configuration is needed.

        Returns
        -------
        GuildSpamSettings
            The last committed snapshot, or disabled defaults for a new guild.
        """

        return self._cache.get(guild_id, self._default)

    async def update(
        self,
        guild: discord.Guild,
        *,
        updated_by_id: int,
        settings: SpamSettings | None = None,
        enabled: bool | None = None,
        changes: dict[str, object] | None = None,
        rule: SpamRule | None = None,
    ) -> GuildSpamSettings:
        """Validate and save settings while holding the guild's shared lock.

        Parameters
        ----------
        guild : discord.Guild
            Guild whose settings and bot permissions should be checked.
        updated_by_id : int
            User responsible for the configuration change.
        settings : SpamSettings or None, optional
            Complete replacement settings when no partial changes are supplied.
        enabled : bool or None, optional
            Requested enabled state, or ``None`` to retain the current state.
        changes : dict[str, object] or None, optional
            Partial settings merged against the latest snapshot under the lock.
        rule : SpamRule or None, optional
            Rule to update; ``None`` applies changes to general enforcement settings.

        Returns
        -------
        GuildSpamSettings
            The configuration snapshot published after a successful commit.

        Raises
        ------
        ValueError
            No rules are enabled or the bot lacks required permissions.
        pydantic.ValidationError
            A supplied setting is outside its allowed range.

        Notes
        -----
        Actual changes clear detection state in the same transaction. Requests
        matching an existing snapshot return without resetting its counters.
        """

        async with self.manager.lock(guild.id):
            current = self.get(guild.id)

            # Merge under the lock so concurrent commands cannot lose each other's edits
            if changes:
                settings = self._merge_settings(current.settings, changes, rule)

            updated = replace(
                current,
                settings=current.settings if settings is None else settings,
                enabled=current.enabled if enabled is None else enabled,
            )

            # Disabled configurations can be prepared before permissions are available
            if updated.enabled:
                if not any(
                    updated.settings.rule(rule).enabled for rule in self.manager.rules
                ):
                    raise ValueError('Enable at least one spam rule first')
                validate_permissions(guild, updated.settings)

            if updated == current and guild.id in self._cache:
                return current

            # Configuration and counter resets must become durable together
            async with self.manager.bot.db_session_maker() as session:
                await session.merge(
                    GuildSpamConfig(
                        guild_id=guild.id,
                        enabled=updated.enabled,
                        settings_json=updated.settings.model_dump_json(),
                        updated_by_id=updated_by_id,
                    )
                )
                await self.manager.store.clear(session, guild.id)
                await session.commit()

            # Readers continue seeing the old snapshot if any database operation fails
            self._cache[guild.id] = updated
            self.manager.clear_memory(guild.id)

            return updated

    @staticmethod
    def _merge_settings(
        current: SpamSettings,
        changes: dict[str, object],
        rule: SpamRule | None,
    ) -> SpamSettings:
        """Validate partial general settings or one nested rule configuration."""

        values = current.model_dump()

        if rule is None:
            values.update(changes)
        else:
            rate = current.rule(rule).model_dump()
            rate.update(changes)
            values[rule.value.replace('-', '_')] = RateSettings.model_validate(
                rate
            ).model_dump()

        return SpamSettings.model_validate(values)

    async def exempt(
        self,
        guild_id: int,
        kind: ExemptionKind,
        entity_id: int,
        *,
        add: bool,
        updated_by_id: int,
    ) -> None:
        """Add or remove an exemption and clear counters containing prior contributions.

        Parameters
        ----------
        guild_id : int
            Guild whose explicit exemptions should be changed.
        kind : ExemptionKind
            Whether the entity is a user, role, or channel.
        entity_id : int
            Positive Discord ID of the entity.
        add : bool
            Add the exemption when true; remove it when false.
        updated_by_id : int
            User responsible for this change.

        Raises
        ------
        ValueError
            The entity ID is not positive.
        """

        if entity_id <= 0:
            raise ValueError('Entity IDs must be positive')

        async with self.manager.lock(guild_id):
            current = self.get(guild_id)
            entities = set(current.exemptions)

            if add:
                entities.add((kind, entity_id))
            else:
                entities.discard((kind, entity_id))

            async with self.manager.bot.db_session_maker() as session:
                row = GuildSpamExemption(
                    guild_id=guild_id, kind=kind.value, entity_id=entity_id
                )
                if add:
                    await session.merge(row)
                else:
                    await session.execute(
                        delete(GuildSpamExemption).where(
                            col(GuildSpamExemption.guild_id) == guild_id,
                            col(GuildSpamExemption.kind) == kind.value,
                            col(GuildSpamExemption.entity_id) == entity_id,
                        )
                    )

                # Preserve settings while recording who changed the exemption list
                await session.merge(
                    GuildSpamConfig(
                        guild_id=guild_id,
                        enabled=current.enabled,
                        settings_json=current.settings.model_dump_json(),
                        updated_by_id=updated_by_id,
                    )
                )
                await self.manager.store.clear(session, guild_id)
                await session.commit()

            self._cache[guild_id] = replace(current, exemptions=frozenset(entities))
            self.manager.clear_memory(guild_id)

    async def delete(self, guild_id: int) -> None:
        """Remove a departed guild's configuration and counters, retaining incidents.

        Parameters
        ----------
        guild_id : int
            Guild whose active service data should be removed.
        """

        async with self.manager.lock(guild_id):
            async with self.manager.bot.db_session_maker() as session:
                await session.execute(
                    delete(GuildSpamExemption).where(
                        col(GuildSpamExemption.guild_id) == guild_id
                    )
                )
                await session.execute(
                    delete(GuildSpamConfig).where(
                        col(GuildSpamConfig.guild_id) == guild_id
                    )
                )
                await self.manager.store.clear(session, guild_id)
                await session.commit()

            self._cache.pop(guild_id, None)
            self.manager.clear_memory(guild_id)

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import discord
from sqlmodel import col, select

from moist_bot.models import (
    BLOCKLIST_SENTINEL_ID,
    BlocklistEntry,
    BlocklistScope,
    BlocklistSource,
    ChannelPolicyMode,
    GuildChannelPolicy,
    GuildChannelPolicyChannel,
)
from moist_bot.settings import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


log = logging.getLogger('discord.' + __name__)


@dataclass(frozen=True, slots=True)
class ChannelPolicy:
    """Cached command channel policy for a guild.

    Parameters
    ----------
    mode:
        Whether the configured channels are ignored, denied, or allowed.
    channel_ids:
        Channel IDs attached to the policy mode.
    """

    mode: ChannelPolicyMode
    channel_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class BlocklistDecision:
    """Result returned when a blocklist rule blocks a command.

    Parameters
    ----------
    reason:
        Human-readable reason for logs and interaction responses.
    scope:
        Stable scope key used for log throttling.
    """

    reason: str
    scope: str


class BlocklistManager:
    """Manage persistent blocklist state and fast runtime checks.

    The database is the source of truth, while the sets in this class are the
    hot path used by command checks. Command mutations update both so regular
    invocations do not need to query SQLite.
    """

    def __init__(self, bot: MoistBot) -> None:
        """Create an empty blocklist cache.

        Parameters
        ----------
        bot:
            Bot instance that owns the database/session maker.
        """

        self.bot: MoistBot = bot
        self.global_users: set[int] = set()
        self.guilds: set[int] = set()
        self.guild_users: set[tuple[int, int]] = set()
        self.channel_policies: dict[int, ChannelPolicy] = {}

    async def load(self) -> None:
        """Load all blocklist rows and channel policies into memory."""

        async with self.bot.db_session_maker() as session:
            entries = await self._fetch_entries(session)
            policies = await self._fetch_channel_policies(session)

        # Swap the whole cache after reads complete so partial load failures do
        # not leave the manager with a half-updated view of the database
        self.global_users.clear()
        self.guilds.clear()
        self.guild_users.clear()
        self.channel_policies = policies

        for entry in entries:
            self._cache_entry(entry)

        log.info(
            f'Loaded {len(entries)} blocklist entries and {len(policies)} channel policies.'
        )

    async def _fetch_entries(self, session: AsyncSession) -> list[BlocklistEntry]:
        """Return every blocklist entry from the database."""

        result = await session.execute(select(BlocklistEntry))
        return list(result.scalars().all())

    async def _fetch_channel_policies(
        self, session: AsyncSession
    ) -> dict[int, ChannelPolicy]:
        """Return cached channel policies keyed by guild ID."""

        policies_result = await session.execute(select(GuildChannelPolicy))
        channels_result = await session.execute(select(GuildChannelPolicyChannel))

        channel_ids_by_guild: dict[int, set[int]] = {}
        for row in channels_result.scalars().all():
            channel_ids_by_guild.setdefault(row.guild_id, set()).add(row.channel_id)

        policies: dict[int, ChannelPolicy] = {}
        for policy in policies_result.scalars().all():
            mode = self._normalize_channel_mode(policy.mode)
            policies[policy.guild_id] = ChannelPolicy(
                mode=mode,
                channel_ids=frozenset(channel_ids_by_guild.get(policy.guild_id, ())),
            )
        return policies

    @staticmethod
    def _normalize_channel_mode(mode: str) -> ChannelPolicyMode:
        """Coerce database strings into supported channel policy modes."""

        try:
            return ChannelPolicyMode(mode)
        except ValueError:
            return ChannelPolicyMode.OFF

    def _cache_entry(self, entry: BlocklistEntry) -> None:
        """Add a database entry to the in-memory cache."""

        if entry.scope == BlocklistScope.GLOBAL_USER:
            self.global_users.add(entry.user_id)
        elif entry.scope == BlocklistScope.GUILD:
            self.guilds.add(entry.guild_id)
        elif entry.scope == BlocklistScope.GUILD_USER:
            self.guild_users.add((entry.guild_id, entry.user_id))

    def _uncache_entry(
        self, scope: BlocklistScope, guild_id: int, user_id: int
    ) -> None:
        """Remove an entry from the in-memory cache."""

        if scope == BlocklistScope.GLOBAL_USER:
            self.global_users.discard(user_id)
        elif scope == BlocklistScope.GUILD:
            self.guilds.discard(guild_id)
        elif scope == BlocklistScope.GUILD_USER:
            self.guild_users.discard((guild_id, user_id))

    async def upsert_entry(
        self,
        *,
        scope: BlocklistScope,
        guild_id: int = BLOCKLIST_SENTINEL_ID,
        user_id: int = BLOCKLIST_SENTINEL_ID,
        created_by_id: int | None,
        source: BlocklistSource = BlocklistSource.MANUAL,
        reason: str | None = None,
    ) -> bool:
        """Insert or update a blocklist entry.

        Parameters
        ----------
        scope:
            Blocklist scope to mutate.
        guild_id:
            Guild ID for guild-scoped entries, or the sentinel for global rows.
        user_id:
            User ID for user-scoped entries, or the sentinel for guild rows.
        created_by_id:
            User ID that caused the mutation. ``None`` means an automatic action.
        source:
            Whether this entry was created manually or automatically.
        reason:
            Optional human-readable reason.

        Returns
        -------
        bool
            ``True`` if a row was created, ``False`` if an existing row changed.
        """

        async with self.bot.db_session_maker() as session:
            existing = await self._get_entry(
                session,
                scope=scope,
                guild_id=guild_id,
                user_id=user_id,
            )
            created = existing is None
            if existing is None:
                existing = BlocklistEntry(
                    scope=scope,
                    guild_id=guild_id,
                    user_id=user_id,
                )
                session.add(existing)

            existing.created_at = discord.utils.utcnow()
            existing.created_by_id = created_by_id
            existing.source = source
            existing.reason = reason
            await session.commit()

        self._cache_entry(existing)
        return created

    async def remove_entry(
        self,
        *,
        scope: BlocklistScope,
        guild_id: int = BLOCKLIST_SENTINEL_ID,
        user_id: int = BLOCKLIST_SENTINEL_ID,
    ) -> bool:
        """Remove a blocklist entry.

        Returns
        -------
        bool
            ``True`` when a row existed and was removed.
        """

        async with self.bot.db_session_maker() as session:
            entry = await self._get_entry(
                session,
                scope=scope,
                guild_id=guild_id,
                user_id=user_id,
            )
            if entry is None:
                return False

            await session.delete(entry)
            await session.commit()

        self._uncache_entry(scope, guild_id, user_id)
        return True

    async def _get_entry(
        self,
        session: AsyncSession,
        *,
        scope: BlocklistScope,
        guild_id: int,
        user_id: int,
    ) -> BlocklistEntry | None:
        """Fetch one blocklist entry by its unique logical key."""

        statement = select(BlocklistEntry).where(
            col(BlocklistEntry.scope) == scope,
            col(BlocklistEntry.guild_id) == guild_id,
            col(BlocklistEntry.user_id) == user_id,
        )
        result = await session.execute(statement)
        return result.scalar_one_or_none()

    async def entries_for_scope(
        self,
        scope: BlocklistScope,
        *,
        guild_id: int | None = None,
    ) -> list[BlocklistEntry]:
        """Return entries for a command list view."""

        criteria = [col(BlocklistEntry.scope) == scope]
        if guild_id is not None:
            criteria.append(col(BlocklistEntry.guild_id) == guild_id)

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(BlocklistEntry)
                .where(*criteria)
                .order_by(col(BlocklistEntry.created_at).desc())
            )
            return list(result.scalars().all())

    async def set_channel_mode(
        self,
        *,
        guild_id: int,
        mode: ChannelPolicyMode,
        updated_by_id: int | None,
    ) -> None:
        """Create or update a guild channel policy mode."""

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildChannelPolicy).where(
                    col(GuildChannelPolicy.guild_id) == guild_id
                )
            )
            policy = result.scalar_one_or_none()
            if policy is None:
                policy = GuildChannelPolicy(guild_id=guild_id)
                session.add(policy)

            policy.mode = mode
            policy.updated_at = discord.utils.utcnow()
            policy.updated_by_id = updated_by_id
            await session.commit()

        current = self.channel_policies.get(guild_id)
        channel_ids: frozenset[int] = (
            frozenset() if current is None else current.channel_ids
        )
        self.channel_policies[guild_id] = ChannelPolicy(
            mode=mode,
            channel_ids=channel_ids,
        )

    async def add_channel(
        self,
        *,
        guild_id: int,
        channel_id: int,
    ) -> bool:
        """Add a channel to a guild channel policy.

        Returns
        -------
        bool
            ``True`` when the channel was newly added.
        """

        async with self.bot.db_session_maker() as session:
            await self._ensure_channel_policy(session, guild_id)
            result = await session.execute(
                select(GuildChannelPolicyChannel).where(
                    col(GuildChannelPolicyChannel.guild_id) == guild_id,
                    col(GuildChannelPolicyChannel.channel_id) == channel_id,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                return False

            session.add(
                GuildChannelPolicyChannel(
                    guild_id=guild_id,
                    channel_id=channel_id,
                )
            )
            await session.commit()

        self._cache_policy_channel(guild_id, channel_id, add=True)
        return True

    async def _ensure_channel_policy(
        self,
        session: AsyncSession,
        guild_id: int,
    ) -> None:
        """Create the parent policy row before channel rows are attached."""

        result = await session.execute(
            select(GuildChannelPolicy).where(
                col(GuildChannelPolicy.guild_id) == guild_id
            )
        )
        if result.scalar_one_or_none() is None:
            session.add(GuildChannelPolicy(guild_id=guild_id))

    async def remove_channel(
        self,
        *,
        guild_id: int,
        channel_id: int,
    ) -> bool:
        """Remove a channel from a guild channel policy.

        Returns
        -------
        bool
            ``True`` when the channel existed and was removed.
        """

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildChannelPolicyChannel).where(
                    col(GuildChannelPolicyChannel.guild_id) == guild_id,
                    col(GuildChannelPolicyChannel.channel_id) == channel_id,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                return False

            await session.delete(existing)
            await session.commit()

        self._cache_policy_channel(guild_id, channel_id, add=False)
        return True

    def _cache_policy_channel(
        self, guild_id: int, channel_id: int, *, add: bool
    ) -> None:
        """Update one channel ID in the cached policy for a guild."""

        current = self.channel_policies.get(guild_id)
        if current is None:
            current = ChannelPolicy(mode=ChannelPolicyMode.OFF, channel_ids=frozenset())

        channel_ids = set(current.channel_ids)
        if add:
            channel_ids.add(channel_id)
        else:
            channel_ids.discard(channel_id)

        self.channel_policies[guild_id] = ChannelPolicy(
            mode=current.mode,
            channel_ids=frozenset(channel_ids),
        )

    def get_channel_policy(self, guild_id: int) -> ChannelPolicy:
        """Return the cached policy for a guild, defaulting to no restrictions."""

        return self.channel_policies.get(
            guild_id,
            ChannelPolicy(mode=ChannelPolicyMode.OFF, channel_ids=frozenset()),
        )

    def is_guild_blocklisted(self, guild_id: int) -> bool:
        """Return whether an entire guild is blocklisted."""

        return guild_id in self.guilds

    def check_ids(  # noqa: PLR0911
        self,
        *,
        user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        bypass_guild_rules: bool,
    ) -> BlocklistDecision | None:
        """Check raw Discord IDs against cached blocklist rules."""

        if user_id in self.global_users:
            return BlocklistDecision('User is globally blocklisted.', 'global_user')

        if guild_id is None or bypass_guild_rules:
            return None

        if guild_id in self.guilds:
            return BlocklistDecision('Guild is blocklisted.', 'guild')

        if (guild_id, user_id) in self.guild_users:
            return BlocklistDecision('User is blocklisted in this guild.', 'guild_user')

        if channel_id is None:
            return None

        # Channel modes are intentionally evaluated last: user and guild
        # blocklists should win over channel allowlist/denylist configuration.
        policy = self.get_channel_policy(guild_id)
        if (
            policy.mode == ChannelPolicyMode.DENYLIST
            and channel_id in policy.channel_ids
        ):
            return BlocklistDecision('Channel is denylisted.', 'channel_denylist')
        if (
            policy.mode == ChannelPolicyMode.ALLOWLIST
            and channel_id not in policy.channel_ids
        ):
            return BlocklistDecision('Channel is not allowlisted.', 'channel_allowlist')

        return None

    async def check_context(self, ctx: Context) -> BlocklistDecision | None:
        """Check a prefix or hybrid command context against blocklist rules."""

        if await self.bot.is_owner(ctx.author):
            return None

        guild_id = None if ctx.guild is None else ctx.guild.id
        channel_id = ctx.channel.id
        bypass_guild_rules = (
            isinstance(ctx.author, discord.Member)
            and ctx.author.guild_permissions.manage_guild
        )

        return self.check_ids(
            user_id=ctx.author.id,
            guild_id=guild_id,
            channel_id=channel_id,
            bypass_guild_rules=bypass_guild_rules,
        )

    async def check_interaction(
        self, interaction: discord.Interaction[MoistBot]
    ) -> BlocklistDecision | None:
        """Check an application command interaction against blocklist rules."""

        if await self.bot.is_owner(interaction.user):
            return None

        member = interaction.user
        bypass_guild_rules = (
            isinstance(member, discord.Member) and member.guild_permissions.manage_guild
        )

        return self.check_ids(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            bypass_guild_rules=bypass_guild_rules,
        )

    async def log(self, message: str) -> None:
        """Send an automatic blocklist message to the configured logs channel."""

        try:
            channel = self.bot.get_channel(settings.logs_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(settings.logs_channel_id)

            if not hasattr(channel, 'send'):
                return

            messageable = cast('discord.abc.Messageable', channel)
            await messageable.send(message)
        except discord.HTTPException:
            log.exception('Failed to send blocklist log message.')

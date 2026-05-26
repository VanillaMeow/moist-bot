from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import discord
from sqlalchemy import delete
from sqlmodel import col, select

from moist_bot.models import (
    BLOCKLIST_SENTINEL_ID,
    BlocklistEntry,
    BlocklistScope,
    BlocklistSource,
    ChannelPolicyMode,
    GuildChannelPolicy,
    GuildChannelPolicyChannel,
    GuildChannelPolicyPermission,
)
from moist_bot.settings import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context, Interaction


log = logging.getLogger('discord.' + __name__)

VALID_PERMISSION_NAMES: Final[frozenset[str]] = frozenset(
    discord.Permissions.VALID_FLAGS
)
POLICY_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    ('blocklist policy', 'blocklist channel', 'blocklist permission')
)


@dataclass(frozen=True, slots=True)
class ChannelPolicy:
    """Cached command channel policy for a guild.

    Parameters
    ----------
    mode:
        Whether the configured channels are ignored, denied, or allowed.
    channel_ids:
        Channel IDs attached to the policy mode.
    permission_names:
        Discord permission flag names that may use commands under this policy.
    """

    mode: ChannelPolicyMode
    channel_ids: frozenset[int]
    permission_names: frozenset[str]


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


class BlocklistManager:  # noqa: PLR0904
    """Manage persistent blocklist state and fast runtime checks.

    The database is the source of truth, while the sets in this class are the
    hot path used by command checks. Command mutations update both so regular
    invocations do not need to query SQLite.
    """

    def __init__(self, bot: MoistBot) -> None:
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
        permissions_result = await session.execute(select(GuildChannelPolicyPermission))

        channel_ids_by_guild: defaultdict[int, set[int]] = defaultdict(set)
        for row in channels_result.scalars().all():
            channel_ids_by_guild[row.guild_id].add(row.channel_id)

        permission_names_by_guild: defaultdict[int, set[str]] = defaultdict(set)
        for row in permissions_result.scalars().all():
            permission_names_by_guild[row.guild_id].add(row.permission_name)

        policies: dict[int, ChannelPolicy] = {}
        for policy in policies_result.scalars().all():
            mode = self._normalize_channel_mode(policy.mode)
            policies[policy.guild_id] = ChannelPolicy(
                mode=mode,
                channel_ids=frozenset(channel_ids_by_guild.get(policy.guild_id, ())),
                permission_names=frozenset(
                    permission_names_by_guild.get(policy.guild_id, ())
                ),
            )
        return policies

    @staticmethod
    def _normalize_channel_mode(mode: str) -> ChannelPolicyMode:
        """Coerce database strings into supported channel policy modes."""

        try:
            return ChannelPolicyMode(mode)
        except ValueError:
            return ChannelPolicyMode.LOCKED

    @staticmethod
    def normalize_permission_name(permission_name: str) -> str:
        """Return a canonical Discord permission flag name."""

        return permission_name.strip().lower().replace('-', '_').replace(' ', '_')

    @classmethod
    def validate_permission_name(cls, permission_name: str) -> str:
        """Return a valid permission flag name or raise ``ValueError``."""

        normalized = cls.normalize_permission_name(permission_name)
        if normalized not in VALID_PERMISSION_NAMES:
            raise ValueError(permission_name)
        return normalized

    @classmethod
    def validate_permission_names(cls, permission_names: list[str]) -> list[str]:
        """Return unique valid permission flag names in input order."""

        valid_permissions: list[str] = []
        seen: set[str] = set()
        for permission_name in permission_names:
            normalized = cls.validate_permission_name(permission_name)
            if normalized in seen:
                continue

            seen.add(normalized)
            valid_permissions.append(normalized)
        return valid_permissions

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
        permission_names: frozenset[str] = (
            frozenset() if current is None else current.permission_names
        )
        self.channel_policies[guild_id] = ChannelPolicy(
            mode=mode,
            channel_ids=channel_ids,
            permission_names=permission_names,
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

    async def clear_channels(self, *, guild_id: int) -> int:
        """Remove all channels from a guild channel policy."""

        current = self.get_channel_policy(guild_id)
        removed_count = len(current.channel_ids)
        if not removed_count:
            return 0

        async with self.bot.db_session_maker() as session:
            await session.execute(
                delete(GuildChannelPolicyChannel).where(
                    col(GuildChannelPolicyChannel.guild_id) == guild_id
                )
            )
            await session.commit()

        self.channel_policies[guild_id] = ChannelPolicy(
            mode=current.mode,
            channel_ids=frozenset(),
            permission_names=current.permission_names,
        )
        return removed_count

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
        """Updates one channel ID in the cached policy for a guild."""

        current = self.channel_policies.get(guild_id)
        if current is None:
            current = ChannelPolicy(
                mode=ChannelPolicyMode.LOCKED,
                channel_ids=frozenset(),
                permission_names=frozenset(),
            )

        channel_ids = set(current.channel_ids)
        if add:
            channel_ids.add(channel_id)
        else:
            channel_ids.discard(channel_id)

        self.channel_policies[guild_id] = ChannelPolicy(
            mode=current.mode,
            channel_ids=frozenset(channel_ids),
            permission_names=current.permission_names,
        )

    async def add_permission(
        self,
        *,
        guild_id: int,
        permission_name: str,
    ) -> bool:
        """Add a permission flag to a guild channel policy."""

        permission_name = self.validate_permission_name(permission_name)
        async with self.bot.db_session_maker() as session:
            await self._ensure_channel_policy(session, guild_id)
            result = await session.execute(
                select(GuildChannelPolicyPermission).where(
                    col(GuildChannelPolicyPermission.guild_id) == guild_id,
                    col(GuildChannelPolicyPermission.permission_name)
                    == permission_name,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                return False

            session.add(
                GuildChannelPolicyPermission(
                    guild_id=guild_id,
                    permission_name=permission_name,
                )
            )
            await session.commit()

        self._cache_policy_permission(guild_id, permission_name, add=True)
        return True

    async def remove_permission(
        self,
        *,
        guild_id: int,
        permission_name: str,
    ) -> bool:
        """Remove a permission flag from a guild channel policy."""

        permission_name = self.validate_permission_name(permission_name)
        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(GuildChannelPolicyPermission).where(
                    col(GuildChannelPolicyPermission.guild_id) == guild_id,
                    col(GuildChannelPolicyPermission.permission_name)
                    == permission_name,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                return False

            await session.delete(existing)
            await session.commit()

        self._cache_policy_permission(guild_id, permission_name, add=False)
        return True

    async def clear_permissions(self, *, guild_id: int) -> int:
        """Remove all permission flags from a guild channel policy."""

        current = self.get_channel_policy(guild_id)
        removed_count = len(current.permission_names)
        if not removed_count:
            return 0

        async with self.bot.db_session_maker() as session:
            await session.execute(
                delete(GuildChannelPolicyPermission).where(
                    col(GuildChannelPolicyPermission.guild_id) == guild_id
                )
            )
            await session.commit()

        self.channel_policies[guild_id] = ChannelPolicy(
            mode=current.mode,
            channel_ids=current.channel_ids,
            permission_names=frozenset(),
        )
        return removed_count

    def _cache_policy_permission(
        self, guild_id: int, permission_name: str, *, add: bool
    ) -> None:
        """Updates one permission flag in the cached policy for a guild."""

        current = self.channel_policies.get(guild_id)
        if current is None:
            current = ChannelPolicy(
                mode=ChannelPolicyMode.LOCKED,
                channel_ids=frozenset(),
                permission_names=frozenset(),
            )

        permission_names = set(current.permission_names)
        if add:
            permission_names.add(permission_name)
        else:
            permission_names.discard(permission_name)

        self.channel_policies[guild_id] = ChannelPolicy(
            mode=current.mode,
            channel_ids=current.channel_ids,
            permission_names=frozenset(permission_names),
        )

    def get_channel_policy(self, guild_id: int) -> ChannelPolicy:
        """Return the cached policy for a guild, defaulting to locked."""

        return self.channel_policies.get(
            guild_id,
            ChannelPolicy(
                mode=ChannelPolicyMode.LOCKED,
                channel_ids=frozenset(),
                permission_names=frozenset(),
            ),
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
        user_permissions: discord.Permissions | None,
        bypass_access_policy: bool,
    ) -> BlocklistDecision | None:
        """Check raw Discord IDs against cached blocklist rules."""

        if user_id in self.global_users:
            return BlocklistDecision('User is globally blocklisted.', 'global_user')

        if guild_id is None:
            return None

        if guild_id in self.guilds:
            return BlocklistDecision('Guild is blocklisted.', 'guild')

        if (guild_id, user_id) in self.guild_users:
            return BlocklistDecision('User is blocklisted in this guild.', 'guild_user')

        if bypass_access_policy:
            return None

        # Channel modes are intentionally evaluated last: user and guild
        # blocklists should win over channel allowlist/denylist configuration.
        policy = self.get_channel_policy(guild_id)
        if policy.mode == ChannelPolicyMode.LOCKED:
            return BlocklistDecision('Guild policy is locked.', 'guild_policy_locked')

        if (
            policy.mode == ChannelPolicyMode.DENYLIST
            and channel_id is not None
            and channel_id in policy.channel_ids
        ):
            return BlocklistDecision('Channel is denylisted.', 'channel_denylist')
        if policy.mode == ChannelPolicyMode.ALLOWLIST and (
            channel_id is None or channel_id not in policy.channel_ids
        ):
            return BlocklistDecision('Channel is not allowlisted.', 'channel_allowlist')
        if policy.permission_names and not self.has_any_permission(
            user_permissions,
            policy.permission_names,
        ):
            return BlocklistDecision(
                'User is missing a required permission.',
                'guild_policy_permission',
            )

        return None

    @staticmethod
    def has_any_permission(
        permissions: discord.Permissions | None,
        permission_names: frozenset[str],
    ) -> bool:
        """Return whether permissions include any configured policy flag."""

        if permissions is None:
            return False
        return any(
            getattr(permissions, permission_name)
            for permission_name in permission_names
        )

    @classmethod
    def is_policy_command_name(cls, command_name: str | None) -> bool:
        """Return whether a command belongs to the guild access policy surface."""

        if command_name is None:
            return False
        return any(
            command_name == name or command_name.startswith(f'{name} ')
            for name in POLICY_COMMAND_NAMES
        )

    @staticmethod
    def _can_manage_policy(member: discord.abc.User) -> bool:
        return (
            isinstance(member, discord.Member) and member.guild_permissions.manage_guild
        )

    @staticmethod
    def _permissions_for_context(ctx: Context) -> discord.Permissions | None:
        if not isinstance(ctx.author, discord.Member):
            return None
        if not hasattr(ctx.channel, 'permissions_for'):
            return None

        return ctx.channel.permissions_for(ctx.author)

    @staticmethod
    def _permissions_for_interaction(
        interaction: Interaction,
    ) -> discord.Permissions | None:
        interaction_permissions = getattr(interaction, 'permissions', None)
        if isinstance(interaction_permissions, discord.Permissions):
            return interaction_permissions

        if not isinstance(interaction.user, discord.Member):
            return None

        channel = interaction.channel
        if channel is None or not hasattr(channel, 'permissions_for'):
            return None

        return channel.permissions_for(interaction.user)

    async def check_context(self, ctx: Context) -> BlocklistDecision | None:
        """Check a prefix or hybrid command context against blocklist rules."""

        if await self.bot.is_owner(ctx.author):
            return None

        command_name = None if ctx.command is None else ctx.command.qualified_name
        bypass_access_policy = self._can_manage_policy(
            ctx.author
        ) and self.is_policy_command_name(command_name)

        return self.check_ids(
            user_id=ctx.author.id,
            guild_id=None if ctx.guild is None else ctx.guild.id,
            channel_id=ctx.channel.id,
            user_permissions=self._permissions_for_context(ctx),
            bypass_access_policy=bypass_access_policy,
        )

    async def check_interaction(
        self, interaction: Interaction
    ) -> BlocklistDecision | None:
        """Check an application command interaction against blocklist rules."""

        if await self.bot.is_owner(interaction.user):
            return None

        command = interaction.command
        command_name = None if command is None else command.qualified_name

        bypass_access_policy = self._can_manage_policy(
            interaction.user
        ) and self.is_policy_command_name(command_name)

        return self.check_ids(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_permissions=self._permissions_for_interaction(interaction),
            bypass_access_policy=bypass_access_policy,
        )

    async def log(self, message: str) -> None:
        """Send an automatic blocklist message to the configured logs channel."""

        try:
            channel = await self.bot.get_or_fetch_channel(settings.logs_channel_id)
        except discord.HTTPException:
            log.exception('Failed to send blocklist log message.')
            return

        if not hasattr(channel, 'send'):
            return

        # Whatever the hell is going on with pyright here...
        messageable = cast('discord.abc.Messageable', channel)
        await messageable.send(message)

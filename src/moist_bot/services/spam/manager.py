"""Coordinate spam detection, durable state, and Discord moderation actions."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from time import time
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import discord
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import col, select

from moist_bot.models.spam import SpamIncident

from .actions import SpamActions
from .config import SpamConfig
from .constants import (
    CONTENT_EXCERPT_LIMIT,
    NEW_ACCOUNT_DAYS,
    NEW_MEMBERSHIP_DAYS,
)
from .state import GuildSpamState
from .store import SpamStateStore
from .types import (
    ExemptionKind,
    GuildSpamSettings,
    MessageReference,
    SpamAction,
    SpamRule,
    StateKey,
)

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot

    from .bucket import SavedBucket
    from .types import SpamVerdict


log = logging.getLogger('discord.' + __name__)


class SpamManager:
    """Own spam configuration and counters for the lifetime of the bot.

    Guild locks serialize shared counters and configuration writes. Member locks
    prevent overlapping punishments while allowing other members to be checked.
    """

    rules: tuple[SpamRule, ...] = tuple(SpamRule)

    def __init__(self, bot: MoistBot) -> None:
        """Create the service's components without loading persistent state."""

        self.bot = bot

        # Components share the bot's database session factory
        self.config = SpamConfig(self)
        self.store = SpamStateStore(bot.db_session_maker)
        self.actions = SpamActions()

        # Counters stay cached between messages and configuration cog reloads
        self._state: dict[int, dict[StateKey, SavedBucket]] = {}

        # Weak references release locks after their last waiter finishes
        self._guild_locks = WeakValueDictionary[int, asyncio.Lock]()
        self._member_locks = WeakValueDictionary[tuple[int, int], asyncio.Lock]()

        # Shutdown drains active handlers before the database is disposed
        self._tasks: set[asyncio.Task[object]] = set()
        self._closing = False
        self._loaded = False

    def lock(self, guild_id: int) -> asyncio.Lock:
        """Return the lock shared by a guild's counters and configuration.

        Parameters
        ----------
        guild_id : int
            Guild whose state needs exclusive access.

        Returns
        -------
        asyncio.Lock
            The existing lock while it is in use, or a new idle lock.
        """

        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    def clear_memory(self, guild_id: int) -> None:
        """Discard a guild's cached counters after its durable state is cleared.

        Parameters
        ----------
        guild_id : int
            Guild whose counter snapshot should be removed.

        Notes
        -----
        The caller must hold the guild lock and commit the database change first.
        """

        self._state.pop(guild_id, None)

    async def load(self) -> None:
        """Restore unexpired counters and reconcile actions interrupted by restart.

        Notes
        -----
        Call during startup before accepting messages. Interrupted actions keep
        an unknown outcome because Discord may have applied them before a crash.
        """

        await self.config.load()
        now = time()
        state = await self.store.load(now)

        async with self.bot.db_session_maker() as session:
            for guild_id, buckets in state.items():
                config = self.config.get(guild_id)

                if not config.enabled:
                    await self.store.clear(session, guild_id)
                    buckets.clear()
                    continue

                working = GuildSpamState(
                    settings=config.settings, buckets=buckets, now=now
                )
                working.enforce_capacity()
                await self.store.save(session, guild_id, working)
                state[guild_id] = working.snapshot()

            # A crash between Discord and the final write leaves the outcome unknown
            await session.execute(
                update(SpamIncident)
                .where(col(SpamIncident.status) == 'pending')
                .values(
                    status='interrupted',
                    error='Restart interrupted action; outcome unknown',
                )
            )
            await session.commit()

        # Publish restored state only after its cleanup transaction succeeds
        self._state = state
        self._loaded = True
        log.info(f'Loaded spam settings and persistent state for {len(state)} guilds')

    async def close(self) -> None:
        """Stop accepting messages and wait for active handlers to finish."""

        self._closing = True

        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def is_exempt(
        self, message: discord.Message, config: GuildSpamSettings
    ) -> bool:
        """Check automatic exemptions, configured exemptions, and honeypot priority.

        Parameters
        ----------
        message : discord.Message
            Message being considered for moderation.
        config : GuildSpamSettings
            Configuration snapshot whose explicit exemptions should be checked.

        Returns
        -------
        bool
            Whether the message must bypass spam detection and enforcement.
        """

        member = message.author
        guild = message.guild

        # Only ordinary messages from human guild members reach the detector
        if (
            guild is None
            or message.webhook_id is not None
            or message.is_system()
            or not isinstance(member, discord.Member)
            or member.bot
        ):
            return True

        # Guild staff and bot owners bypass every automatic spam rule
        if (
            member.id == guild.owner_id
            or member.guild_permissions.administrator
            or member.guild_permissions.manage_messages
            or await self.bot.is_owner(member)
        ):
            return True

        exemptions = config.exemptions
        channel_ids = self._channel_ids(message)

        if (
            (ExemptionKind.USER, member.id) in exemptions
            or any((ExemptionKind.ROLE, role.id) in exemptions for role in member.roles)
            or any(
                (ExemptionKind.CHANNEL, channel_id) in exemptions
                for channel_id in channel_ids
            )
        ):
            return True

        # Honeypot owns its channel so the two services cannot punish one trigger
        honeypot = self.bot.honeypot.config.get(guild.id)
        return bool(
            honeypot and honeypot.enabled and honeypot.channel_id == message.channel.id
        )

    @staticmethod
    def _channel_ids(message: discord.Message) -> set[int]:
        """Collect the channel, parent, and category IDs relevant to exemptions."""

        channel_ids = {message.channel.id}

        if isinstance(message.channel, discord.Thread):
            if message.channel.parent_id:
                channel_ids.add(message.channel.parent_id)

            parent = message.channel.parent
            if parent is not None and parent.category_id is not None:
                channel_ids.add(parent.category_id)
        elif (
            isinstance(message.channel, discord.abc.GuildChannel)
            and message.channel.category_id is not None
        ):
            channel_ids.add(message.channel.category_id)

        return channel_ids

    async def handle_message(self, message: discord.Message) -> bool:
        """Process a live message while coordinating shutdown and member actions.

        Parameters
        ----------
        message : discord.Message
            Incoming message from the bot's message event.

        Returns
        -------
        bool
            Whether prefix-command processing should stop for this message.
            Persistence failures also suppress commands without punishing anyone.
        """

        if self._closing or not self._loaded or message.guild is None:
            return False

        config = self.config.get(message.guild.id)
        if not config.enabled or await self.is_exempt(message, config):
            return False
        if not isinstance(message.author, discord.Member):
            return False

        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)

        try:
            member_key = (message.guild.id, message.author.id)
            member_lock = self._member_locks.setdefault(member_key, asyncio.Lock())
            async with member_lock:
                return await self._handle(message, message.author)
        except SQLAlchemyError:
            log.exception(f'Failed to persist spam state in guild {message.guild.id}')
            return True
        finally:
            if task is not None:
                self._tasks.discard(task)

    async def _handle(self, message: discord.Message, member: discord.Member) -> bool:
        """Commit a detection snapshot before running any Discord action."""

        guild = member.guild

        async with self.lock(guild.id):
            # Recheck configuration after waiting for the guild's shared state
            config = self.config.get(guild.id)
            if not config.enabled or await self.is_exempt(message, config):
                return False

            now = time()
            timestamp = message.created_at.timestamp()
            longest_period = max(
                config.settings.rule(rule).period for rule in self.rules
            )
            if not 0 <= now - timestamp <= longest_period:
                return False

            # Work on a private snapshot so failed commits leave the cache intact
            reference = MessageReference(
                message_id=message.id,
                channel_id=message.channel.id,
                user_id=member.id,
                created_at=timestamp,
            )
            working = GuildSpamState(
                settings=config.settings,
                buckets=self._state.get(guild.id, {}),
                now=now,
            )

            is_new = (
                member.created_at
                > message.created_at - timedelta(days=NEW_ACCOUNT_DAYS)
                and member.joined_at is not None
                and member.joined_at
                > message.created_at - timedelta(days=NEW_MEMBERSHIP_DAYS)
            )

            verdict = working.check(
                reference,
                message.content,
                is_new=is_new,
            )

            # Reserve the cooldown and incident in the same transaction as counters
            incident: SpamIncident | None = None
            if verdict is not None and working.reserve_action(member.id):
                incident = self._incident(message, member, verdict, config)

            working.enforce_capacity()

            # Discord must not see a punishment until its detection is durable
            async with self.bot.db_session_maker() as session:
                await self.store.save(session, guild.id, working)
                if incident is not None:
                    session.add(incident)

                await session.commit()

            self._state[guild.id] = working.snapshot()

        # Release the shared counter lock before awaiting Discord requests
        return await self._finish_incident(message, member, verdict, config, incident)

    async def _finish_incident(
        self,
        message: discord.Message,
        member: discord.Member,
        verdict: SpamVerdict | None,
        config: GuildSpamSettings,
        incident: SpamIncident | None,
    ) -> bool:
        """Enforce a committed detection, save its outcome, and report it."""

        if verdict is None:
            return False

        suppress_command = config.settings.action != SpamAction.ALERT
        if incident is None:
            return suppress_command

        guild = member.guild

        # Configuration may have changed while persistence or lock acquisition awaited
        if self.config.get(guild.id) != config or await self.is_exempt(message, config):
            incident.status = 'skipped'
            incident.error = 'Configuration or exemptions changed before enforcement'
        else:
            outcome = await self.actions.apply(member, verdict, config.settings)
            incident.status = 'succeeded' if outcome.succeeded else 'failed'
            incident.error = outcome.error
            incident.messages_deleted = outcome.deleted
            incident.cleanup_error = outcome.cleanup_error

        # Save punishment and cleanup results independently of the initial reservation
        async with self.bot.db_session_maker() as session:
            await session.merge(incident)
            await session.commit()

        await self.actions.report(guild, config.settings, incident)
        return suppress_command

    def _incident(
        self,
        message: discord.Message,
        member: discord.Member,
        verdict: SpamVerdict,
        config: GuildSpamSettings,
    ) -> SpamIncident:
        """Build a pending incident before any punishment has been attempted."""

        return SpamIncident(
            guild_id=member.guild.id,
            user_id=member.id,
            channel_id=message.channel.id,
            message_id=message.id,
            rule=verdict.rule.value,
            action=config.settings.action.value,
            reason=verdict.reason,
            content_excerpt=message.content[:CONTENT_EXCERPT_LIMIT],
        )

    async def incidents(self, guild_id: int, limit: int) -> list[SpamIncident]:
        """Return a guild's newest incidents, including failed and interrupted actions.

        Parameters
        ----------
        guild_id : int
            Guild whose incident history should be read.
        limit : int
            Maximum number of incidents to return.

        Returns
        -------
        list[SpamIncident]
            Recorded incidents ordered from newest to oldest.
        """

        async with self.bot.db_session_maker() as session:
            result = await session.execute(
                select(SpamIncident)
                .where(col(SpamIncident.guild_id) == guild_id)
                .order_by(col(SpamIncident.id).desc())
                .limit(limit)
            )
            return list(result.scalars())

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import time
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from .activity import ActivityTracker
from .reposts import RepostTracker
from .skip_cooldown import SkipCooldown, SkipCooldownState
from .store import ActivitySnapshot, RepostSnapshot, TrackerStateStore

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable
    from typing import Literal

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from moist_bot.types import GuildMessage


@dataclass(frozen=True, slots=True)
class TrackingConfig:
    activity_window: float
    activity_threshold: int

    help_window: float
    help_skip_count: int

    repost_window: float
    repost_similarity: float
    repost_min_length: int
    warning_window: float


class TrackingService:
    """Own tracker state, persistence, restoration and per-user synchronization."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        namespace: str,
        *,
        config: TrackingConfig,
        timer: Callable[[], float] = time,
    ) -> None:
        self._config = config
        self.store = TrackerStateStore(sessions, namespace, timer=timer)

        self.activity = ActivityTracker[tuple[int, int]](
            window=config.activity_window,
            threshold=config.activity_threshold,
            timer=timer,
        )
        self.reposts = RepostTracker[int](
            window=config.repost_window,
            similarity=config.repost_similarity,
            min_length=config.repost_min_length,
            timer=timer,
        )
        self.warnings = ActivityTracker[int](
            window=config.warning_window, threshold=1, timer=timer
        )
        self.help_cooldown = SkipCooldown[int](
            window=config.help_window, skip_count=config.help_skip_count, timer=timer
        )

        # Release idle locks once no operation holds or waits for them
        self._user_locks = WeakValueDictionary[int, asyncio.Lock]()

    def _lock(self, user_id: int) -> asyncio.Lock:
        # Share locks across channels so a user's saves cannot overwrite each other
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    async def load(self) -> None:
        """Restore unexpired state before processing new events."""
        # Each tracker checks event ages again instead of restarting its time window
        for state in await self.store.load():
            match state.tracker:
                case 'activity':
                    channel_id, user_id = state.key.split(':')
                    snapshot = ActivitySnapshot.model_validate_json(state.payload)
                    self.activity.restore(
                        (int(channel_id), int(user_id)), snapshot.timestamps
                    )

                case 'warnings':
                    snapshot = ActivitySnapshot.model_validate_json(state.payload)
                    self.warnings.restore(int(state.key), snapshot.timestamps)

                case 'reposts':
                    reposts = RepostSnapshot.model_validate_json(state.payload)
                    self.reposts.restore(int(state.key), reposts.messages)

                case 'help_cooldown':
                    cooldown = SkipCooldownState.model_validate_json(state.payload)
                    self.help_cooldown.restore(int(state.key), cooldown)

                case _:
                    error = f'Unknown tracker kind: {state.tracker}'
                    raise ValueError(error)

    async def is_active(self, message: GuildMessage) -> bool:
        # Readers must wait for pending saves or rollbacks before inspecting state
        async with self._lock(message.author.id):
            return self.activity.is_active((message.channel.id, message.author.id))

    async def find_repost(self, message: GuildMessage) -> int | None:
        async with self._lock(message.author.id):
            return self.reposts.find_match(message.author.id, message.content)

    async def record_activity(self, message: GuildMessage) -> None:
        channel_id, user_id = message.channel.id, message.author.id

        async with self._lock(user_id):
            await self._record_activity(
                self.activity,
                'activity',
                (channel_id, user_id),
                f'{channel_id}:{user_id}',
                self._config.activity_window,
                recorded_at=message.created_at.timestamp(),
            )

    async def claim_repost_warning(self, message: GuildMessage) -> bool:
        """Atomically reserve and persist a warning before the caller sends it."""
        user_id = message.author.id

        async with self._lock(user_id):
            if self.warnings.is_active(user_id):
                return False

            # Start this cooldown now, rather than at the source message's creation
            await self._record_activity(
                self.warnings,
                'warnings',
                user_id,
                str(user_id),
                self._config.warning_window,
            )

            return True

    async def _record_activity[Key: Hashable](
        self,
        tracker: ActivityTracker[Key],
        kind: Literal['activity', 'warnings'],
        key: Key,
        storage_key: str,
        window: float,
        *,
        recorded_at: float | None = None,
    ) -> None:
        # Keep the prior snapshot so a failed write can be rolled back in memory
        previous = tracker.snapshot(key)
        tracker.record(key, recorded_at=recorded_at)
        timestamps = tracker.snapshot(key)

        if timestamps == previous:
            return

        try:
            await self.store.save(
                kind,
                storage_key,
                ActivitySnapshot(timestamps=timestamps),
                timestamps[-1] + window if timestamps else 0,
            )
        except Exception, asyncio.CancelledError:
            tracker.restore(key, previous)
            raise

    async def record_repost_source(self, message: GuildMessage) -> None:
        user_id = message.author.id

        async with self._lock(user_id):
            previous = self.reposts.snapshot(user_id)
            self.reposts.record(
                user_id,
                message.content,
                message.id,
                recorded_at=message.created_at.timestamp(),
            )
            messages = self.reposts.snapshot(user_id)

            # Short, expired or duplicate source messages may leave history unchanged
            if messages == previous:
                return

            try:
                await self.store.save(
                    'reposts',
                    str(user_id),
                    RepostSnapshot(messages=messages),
                    messages[-1].recorded_at + self._config.repost_window
                    if messages
                    else 0,
                )
            except Exception, asyncio.CancelledError:
                self.reposts.restore(user_id, previous)
                raise

    async def check_help_cooldown(self, message: GuildMessage) -> bool:
        """Advance and persist the skip counter, returning whether to suppress a reply."""
        user_id = message.author.id

        async with self._lock(user_id):
            previous = self.help_cooldown.snapshot(user_id)
            skip = self.help_cooldown.check(user_id)
            state = self.help_cooldown.snapshot(user_id)

            if state is None:
                raise RuntimeError('Cooldown expired before it could be saved')

            # Save skipped triggers too, so restarting cannot reset the skip count
            try:
                await self.store.save(
                    'help_cooldown', str(user_id), state, state.expires_at
                )
            except Exception, asyncio.CancelledError:
                self.help_cooldown.restore(user_id, previous)
                raise

            return skip

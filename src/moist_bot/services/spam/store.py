"""Store spam counter snapshots within the shared tracker-state table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete
from sqlmodel import col, select

from moist_bot.models import TrackerState

from .bucket import BucketState, SavedBucket
from .constants import STATE_NAMESPACE
from .types import SpamRule

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from .state import GuildSpamState
    from .types import StateKey


class SpamStateStore:
    """Read and write counter rows belonging exclusively to the spam namespace."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Keep the async session factory used for startup restoration."""

        self.sessions = sessions

    async def load(self, now: float) -> dict[int, dict[StateKey, SavedBucket]]:
        """Delete expired spam rows and restore the remaining validated snapshots.

        Parameters
        ----------
        now : float
            Current Unix timestamp used to discard expired counters and cooldowns.

        Returns
        -------
        dict[int, dict[StateKey, SavedBucket]]
            Unexpired counter snapshots grouped by guild and counter key.

        Raises
        ------
        ValueError
            A persisted row names an unsupported spam tracker.
        pydantic.ValidationError
            A saved counter payload does not match the expected schema.
        """

        states: dict[int, dict[StateKey, SavedBucket]] = {}

        async with self.sessions() as session:
            # Other services own their own expiry policies in this shared table
            await session.execute(
                delete(TrackerState).where(
                    col(TrackerState.namespace) == STATE_NAMESPACE,
                    col(TrackerState.expires_at) <= now,
                )
            )

            # Load older expiries first so capacity trimming retains newer entries
            result = await session.execute(
                select(TrackerState)
                .where(col(TrackerState.namespace) == STATE_NAMESPACE)
                .order_by(col(TrackerState.expires_at))
            )

            valid_trackers = {rule.value for rule in SpamRule} | {'cooldown'}
            for row in result.scalars():
                if row.tracker not in valid_trackers:
                    error = f'Unknown spam tracker: {row.tracker}'
                    raise ValueError(error)

                guild, key = row.key.split(':', 1)
                bucket = BucketState.model_validate_json(row.payload)
                states.setdefault(int(guild), {})[row.tracker, key] = SavedBucket(
                    bucket=bucket, expires_at=row.expires_at
                )

            await session.commit()

        return states

    async def save(
        self,
        session: AsyncSession,
        guild_id: int,
        state: GuildSpamState,
    ) -> None:
        """Stage changed counter rows in the caller's transaction.

        Parameters
        ----------
        session : AsyncSession
            Transaction that also owns any new incident and action reservation.
        guild_id : int
            Guild whose counter keys are being written.
        state : GuildSpamState
            Working snapshot owning the remaining rows and changed keys.

        Notes
        -----
        This method does not commit. The manager commits counters, cooldowns,
        and incidents together before publishing its cache or calling Discord.
        """

        buckets = state.buckets
        for tracker, key in state.changed:
            entry = buckets.get((tracker, key))
            storage_key = f'{guild_id}:{key}'

            # Missing keys represent expired or capacity-evicted rows
            if entry is None:
                await session.execute(
                    delete(TrackerState).where(
                        col(TrackerState.namespace) == STATE_NAMESPACE,
                        col(TrackerState.tracker) == tracker,
                        col(TrackerState.key) == storage_key,
                    )
                )
            else:
                await session.merge(
                    TrackerState(
                        namespace=STATE_NAMESPACE,
                        tracker=tracker,
                        key=storage_key,
                        payload=entry.bucket.model_dump_json(),
                        expires_at=entry.expires_at,
                    )
                )

    async def clear(self, session: AsyncSession, guild_id: int) -> None:
        """Stage deletion of one guild's spam rows without committing.

        Parameters
        ----------
        session : AsyncSession
            Configuration transaction that will commit the reset.
        guild_id : int
            Guild whose counters and action cooldowns should be removed.
        """

        await session.execute(
            delete(TrackerState).where(
                col(TrackerState.namespace) == STATE_NAMESPACE,
                col(TrackerState.key).startswith(f'{guild_id}:'),
            )
        )

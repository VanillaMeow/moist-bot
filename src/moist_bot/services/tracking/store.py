from __future__ import annotations

from time import time
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, FiniteFloat
from sqlalchemy import delete
from sqlmodel import col, select

from moist_bot.models.tracker_state import TrackerState
from moist_bot.services.tracking.reposts import (
    RecentMessage,  # ruff: ignore[typing-only-first-party-import] - Pydantic field type
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from moist_bot.services.tracking.skip_cooldown import SkipCooldownState

type TrackerKind = Literal['activity', 'reposts', 'warnings', 'help_cooldown']


class ActivitySnapshot(BaseModel):
    timestamps: tuple[FiniteFloat, ...]


class RepostSnapshot(BaseModel):
    messages: tuple[RecentMessage, ...]


class TrackerStateStore:
    """Save tracker updates immediately and periodically remove expired rows."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        namespace: str,
        *,
        timer: Callable[[], float] = time,
    ) -> None:
        self._sessions = sessions
        self._namespace = namespace
        self._timer = timer
        self._next_cleanup = 0.0

    async def load(self) -> Sequence[TrackerState]:
        current = self._timer()

        async with self._sessions() as session:
            # Remove state that expired while the bot was offline
            await session.execute(
                delete(TrackerState).where(col(TrackerState.expires_at) <= current)
            )

            # Load older entries first so bounded caches retain the newest state
            result = await session.execute(
                select(TrackerState)
                .where(col(TrackerState.namespace) == self._namespace)
                .order_by(col(TrackerState.expires_at))
            )
            states = result.scalars().all()
            await session.commit()

        self._next_cleanup = current + 60
        return states

    async def save(
        self,
        tracker: TrackerKind,
        key: str,
        payload: ActivitySnapshot | RepostSnapshot | SkipCooldownState,
        expires_at: float,
    ) -> None:
        current = self._timer()

        async with self._sessions() as session:
            # Piggyback cleanup on writes, at most once per minute in serial use
            if current >= self._next_cleanup:
                await session.execute(
                    delete(TrackerState).where(col(TrackerState.expires_at) <= current)
                )

            # Empty histories use an expired timestamp to remove their saved row
            if expires_at <= current:
                await session.execute(
                    delete(TrackerState).where(
                        col(TrackerState.namespace) == self._namespace,
                        col(TrackerState.tracker) == tracker,
                        col(TrackerState.key) == key,
                    )
                )
            else:
                await session.merge(
                    TrackerState(
                        namespace=self._namespace,
                        tracker=tracker,
                        key=key,
                        payload=payload.model_dump_json(),
                        expires_at=expires_at,
                    )
                )

            # Commit before returning so callers can safely proceed with a reply
            await session.commit()

        if current >= self._next_cleanup:
            self._next_cleanup = current + 60

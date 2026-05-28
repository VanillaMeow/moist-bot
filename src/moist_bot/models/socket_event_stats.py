# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import UniqueConstraint, func
from sqlmodel import Field, SQLModel, col, select

if TYPE_CHECKING:
    from collections import Counter

    from sqlalchemy.ext.asyncio import AsyncSession


class SocketEventStats(SQLModel, table=True):
    """Precomputed all-time Discord gateway event counters."""

    __tablename__: ClassVar[str] = 'socket_event_stats'
    __table_args__: ClassVar = (
        UniqueConstraint('event_type', name='uq_socket_event_stats_event_type'),
    )

    id: int | None = Field(default=None, primary_key=True)
    event_type: str = Field(max_length=100)
    total_events: int = Field(default=0)

    @classmethod
    async def upsert_event_batch(
        cls,
        session: AsyncSession,
        event_counts: Counter[str],
    ) -> None:
        """Insert or increment precomputed stats for a gateway event batch."""

        event_type = col(cls.event_type)
        result = await session.execute(
            select(cls).where(event_type.in_(tuple(event_counts)))
        )

        existing_by_event = {row.event_type: row for row in result.scalars().all()}
        for event_name, count in event_counts.items():
            if count <= 0:
                continue

            existing = existing_by_event.get(event_name)
            if existing is None:
                session.add(cls(event_type=event_name, total_events=count))
                continue

            existing.total_events += count

    @classmethod
    async def total_events_count(cls, session: AsyncSession) -> int:
        """Return the all-time total gateway event count."""

        total_events = func.coalesce(func.sum(col(cls.total_events)), 0)
        result = await session.execute(select(total_events))
        return int(result.scalar_one())

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastbloom_rs import BloomFilter
from sqlalchemy import func, select
from sqlmodel import col

from moist_bot.db.constants import DATABASE_STREAM_BATCH_SIZE
from moist_bot.models import HoneypotIncident

from .constants import BLOOM_MIN_EXPECTED_ITEMS

if TYPE_CHECKING:
    from .manager import HoneypotManager


log = logging.getLogger('discord.' + __name__)


class MessageBloomFilter:
    """Bounded-memory maybe-set for handled honeypot messages."""

    __slots__ = (
        '_false_positive_rate',
        '_rebuild_task',
        'bloom',
        'expected_items',
        'inserted_items',
        'manager',
    )

    def __init__(
        self,
        manager: HoneypotManager,
        *,
        false_positive_rate: float = 0.01,
    ) -> None:
        self.manager: HoneypotManager = manager
        self._false_positive_rate: float = false_positive_rate

        self._rebuild_task: asyncio.Task[None] | None = None
        self.expected_items: int = BLOOM_MIN_EXPECTED_ITEMS
        self.inserted_items: int = 0
        self.bloom: BloomFilter = BloomFilter(
            self.expected_items,
            self._false_positive_rate,
        )

    async def load(self) -> None:
        """Load all handled honeypot message keys into the filter."""

        await self._rebuild(growth_factor=1)

    @staticmethod
    def _key(*, guild_id: int, message_id: int) -> bytes:
        return f'{guild_id}:{message_id}'.encode()

    async def add(self, *, guild_id: int, message_id: int) -> None:
        """Add a handled message key to the filter."""

        await self._rebuild_if_needed()
        self.bloom.add_bytes(self._key(guild_id=guild_id, message_id=message_id))
        self.inserted_items += 1

    def cancel(self) -> None:
        """Cancel a pending filter rebuild."""
        if self._rebuild_task is not None and not self._rebuild_task.done():
            self._rebuild_task.cancel()

    def _over_capacity(self) -> bool:
        """Return whether the filter is at or has outgrown its expected item count."""
        return self.inserted_items >= self.expected_items

    def might_contain(self, *, guild_id: int, message_id: int) -> bool:
        """Return whether a handled message key may be in the filter."""
        return self.bloom.contains_bytes(
            self._key(guild_id=guild_id, message_id=message_id)
        )

    def maybe_contained_ids(
        self, *, guild_id: int, message_ids: list[int]
    ) -> list[int]:
        """Return message IDs that may be in the filter."""

        keys = [
            self._key(guild_id=guild_id, message_id=message_id)
            for message_id in message_ids
        ]
        results = self.bloom.contains_bytes_batch(keys, check_type=False)
        return [
            message_id
            for message_id, maybe_present in zip(
                message_ids,
                results,
                strict=True,
            )
            if maybe_present
        ]

    async def _rebuild(
        self,
        *,
        growth_factor: int,
    ) -> None:
        """Build and install a filter from persisted handled-message keys."""

        async with self.manager.bot.db_session_maker() as session:
            item_count = await session.scalar(
                select(func.count()).select_from(HoneypotIncident)
            )
            capacity = max(
                BLOOM_MIN_EXPECTED_ITEMS,
                (item_count or 0) * growth_factor * 2,
            )
            bloom = BloomFilter(capacity, self._false_positive_rate)
            statement = select(
                col(HoneypotIncident.guild_id),
                col(HoneypotIncident.message_id),
            ).execution_options(yield_per=DATABASE_STREAM_BATCH_SIZE)
            result = await session.stream(statement)

            inserted_items = 0
            async for guild_id, message_id in result.tuples():
                bloom.add_bytes(self._key(guild_id=guild_id, message_id=message_id))
                inserted_items += 1

        self.bloom = bloom
        self.expected_items = capacity
        self.inserted_items = inserted_items

    async def _rebuild_if_needed(self) -> None:
        """Wait for an active rebuild or schedule one when at capacity."""

        if self._rebuild_task is not None and not self._rebuild_task.done():
            await self._rebuild_task
            return

        if not self._over_capacity():
            return

        self._rebuild_task = asyncio.create_task(self._run_rebuild())

    async def _run_rebuild(self) -> None:
        """Run a scheduled rebuild and handle its task lifecycle."""

        try:
            await self._rebuild(growth_factor=2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Failed to rebuild the handled-message Bloom filter.')
        finally:
            if self._rebuild_task is asyncio.current_task():
                self._rebuild_task = None

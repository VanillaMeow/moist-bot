from __future__ import annotations

from fastbloom_rs import BloomFilter

from .constants import BLOOM_MIN_EXPECTED_ITEMS


class MessageBloomFilter:
    """Bounded-memory maybe-set for handled honeypot messages."""

    __slots__ = ('bloom', 'expected_items', 'inserted_items')

    def __init__(self, *, expected_items: int = 0, false_positive_rate: float = 0.01):
        self.expected_items: int = max(BLOOM_MIN_EXPECTED_ITEMS, expected_items * 2)
        self.inserted_items: int = 0
        self.bloom: BloomFilter = BloomFilter(self.expected_items, false_positive_rate)

    @staticmethod
    def _key(*, guild_id: int, message_id: int) -> bytes:
        return f'{guild_id}:{message_id}'.encode()

    def add(self, *, guild_id: int, message_id: int) -> None:
        """Add a handled message key to the filter."""
        self.bloom.add_bytes(self._key(guild_id=guild_id, message_id=message_id))
        self.inserted_items += 1

    def over_capacity(self) -> bool:
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

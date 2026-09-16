from __future__ import annotations

from collections import deque
from collections.abc import Callable, Hashable, Iterable
from math import isfinite
from time import time

from cachetools import TTLCache


class ActivityTracker[Key: Hashable]:
    """Track whether a key has enough recorded events within a rolling window.

    Event times are Unix timestamps by default. Checking activity
    does not record an event or extend expiry. At capacity, the cache evicts
    the least recently used key, discarding its history.
    """

    def __init__(
        self,
        *,
        window: float,
        threshold: int,
        maxsize: int = 10_000,
        timer: Callable[[], float] = time,
    ):
        if not isfinite(window) or window <= 0:
            raise ValueError('window must be finite and positive')
        if threshold <= 0:
            raise ValueError('threshold must be positive')
        if maxsize <= 0:
            raise ValueError('maxsize must be positive')

        self._window = window
        self._threshold = threshold
        self._timer = timer

        # Cache expiry only reclaims memory; saved event times decide activity
        self._history = TTLCache[Key, deque[float]](maxsize=maxsize, ttl=window)

    def record(self, key: Key, *, recorded_at: float | None = None) -> None:
        """Record an event, optionally using its original creation timestamp."""
        timestamp = self._timer() if recorded_at is None else recorded_at
        self.restore(key, (*self.snapshot(key), timestamp))

    def snapshot(self, key: Key) -> tuple[float, ...]:
        """Return unexpired timestamps suitable for persistence."""
        current = self._timer()

        return tuple(
            timestamp
            for timestamp in self._history.get(key, ())
            if 0 <= current - timestamp < self._window
        )

    def restore(self, key: Key, timestamps: Iterable[float]) -> None:
        """Restore history without renewing the original event timestamps."""
        current = self._timer()

        # Sort by creation time because messages may arrive out of order
        recent = sorted(
            timestamp
            for timestamp in timestamps
            if isfinite(timestamp) and 0 <= current - timestamp < self._window
        )

        if recent:
            self._history[key] = deque(
                recent[-self._threshold :], maxlen=self._threshold
            )
        else:
            self._history.pop(key, None)

    def is_active(self, key: Key) -> bool:
        """Return whether the key meets the threshold within the window."""
        return len(self.snapshot(key)) >= self._threshold

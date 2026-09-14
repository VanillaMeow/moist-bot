from __future__ import annotations

from collections import deque
from collections.abc import Callable, Hashable
from math import isfinite
from time import monotonic

from cachetools import TTLCache


class ActivityTracker[Key: Hashable]:
    """Track whether a key has enough recorded events within a rolling window.

    Times are measured in seconds using a monotonic timer. Checking activity
    does not record an event or extend expiry. At capacity, the cache evicts
    the least recently used key, discarding its history.
    """

    def __init__(
        self,
        *,
        window: float,
        threshold: int,
        maxsize: int = 10_000,
        timer: Callable[[], float] = monotonic,
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
        self._history = TTLCache[Key, deque[float]](
            maxsize=maxsize, ttl=window, timer=timer
        )

    def record(self, key: Key) -> None:
        """Record one event for a key and refresh its history's expiry."""
        timestamps = self._history.get(key)
        if timestamps is None:
            timestamps = deque[float](maxlen=self._threshold)

        # Mutating the deque alone does not refresh the cache entry's expiry
        timestamps.append(self._timer())
        self._history[key] = timestamps

    def is_active(self, key: Key) -> bool:
        """Return whether the key meets the threshold within the window."""
        # All retained events must be recent, so only the oldest needs checking
        timestamps = self._history.get(key)
        return (
            timestamps is not None
            and len(timestamps) >= self._threshold
            and self._timer() - timestamps[0] < self._window
        )

from __future__ import annotations

from collections.abc import Callable, Hashable
from math import isfinite
from time import time

from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class SkipCooldownState(BaseModel):
    model_config = ConfigDict(frozen=True)

    expires_at: FiniteFloat
    skipped: int = Field(default=0, ge=0)


class SkipCooldown[Key: Hashable]:
    """Allow a trigger, skip a fixed count, then allow again or reset on expiry."""

    def __init__(
        self,
        *,
        window: float,
        skip_count: int,
        maxsize: int = 10_000,
        timer: Callable[[], float] = time,
    ) -> None:
        if not isfinite(window) or window <= 0:
            raise ValueError('window must be finite and positive')
        if skip_count < 0 or maxsize <= 0:
            raise ValueError('skip_count must be nonnegative and maxsize positive')

        self._window = window
        self._skip_count = skip_count
        self._timer = timer
        self._states = TTLCache[Key, SkipCooldownState](maxsize=maxsize, ttl=window)

    def check(self, key: Key) -> bool:
        """Advance the counter and return whether this trigger should be skipped."""
        state = self.snapshot(key)
        skip = state is not None and state.skipped < self._skip_count

        # Skips retain the deadline; an allowed trigger starts a new window
        self._states[key] = (
            SkipCooldownState(expires_at=state.expires_at, skipped=state.skipped + 1)
            if skip and state is not None
            else SkipCooldownState(expires_at=self._timer() + self._window)
        )

        return skip

    def snapshot(self, key: Key) -> SkipCooldownState | None:
        state = self._states.get(key)

        # Restoring the cache must not extend a previously saved cooldown deadline
        return state if state is not None and state.expires_at > self._timer() else None

    def restore(self, key: Key, state: SkipCooldownState | None) -> None:
        if state is not None and state.expires_at > self._timer():
            self._states[key] = state
        else:
            self._states.pop(key, None)

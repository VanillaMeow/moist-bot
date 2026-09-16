from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Hashable, Iterable
from difflib import SequenceMatcher
from math import isfinite
from time import time
from unicodedata import normalize

from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict, FiniteFloat


class RecentMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    message_id: int
    recorded_at: FiniteFloat


class RepostTracker[Key: Hashable]:
    """Match text against a key's recent messages without refreshing their expiry."""

    def __init__(
        self,
        *,
        window: float = 600,
        similarity: float = 0.9,
        min_length: int = 20,
        history_size: int = 5,
        maxsize: int = 10_000,
        timer: Callable[[], float] = time,
    ) -> None:
        if not isfinite(window) or window <= 0:
            raise ValueError('window must be finite and positive')
        if not 0 < similarity <= 1:
            raise ValueError('similarity must be greater than zero and at most one')
        if min_length <= 0 or history_size <= 0 or maxsize <= 0:
            raise ValueError('min_length, history_size and maxsize must be positive')

        self._window = window
        self._similarity = similarity
        self._min_length = min_length
        self._history_size = history_size
        self._timer = timer

        self._history = TTLCache[Key, deque[RecentMessage]](maxsize=maxsize, ttl=window)

    @staticmethod
    def _normalize(content: str) -> str:
        # Ignore case, punctuation and spacing changes when comparing reposts
        return re.sub(r'[\W_]+', ' ', normalize('NFKC', content).casefold()).strip()

    def record(
        self,
        key: Key,
        content: str,
        message_id: int,
        *,
        recorded_at: float | None = None,
    ) -> None:
        """Remember source text long enough to compare without storing a message object."""
        content = self._normalize(content)
        if len(content) < self._min_length:
            return

        message = RecentMessage(
            content=content,
            message_id=message_id,
            recorded_at=self._timer() if recorded_at is None else recorded_at,
        )
        self.restore(key, (*self.snapshot(key), message))

    def find_match(self, key: Key, content: str) -> int | None:
        """Return the newest similar source message ID, or None if none matches."""
        content = self._normalize(content)
        if len(content) < self._min_length:
            return None

        for message in reversed(self.snapshot(key)):
            # Exact copies do not need the more expensive fuzzy comparison
            if content == message.content:
                return message.message_id

            matcher = SequenceMatcher(a=message.content, b=content, autojunk=False)
            # These upper bounds reject unlikely matches before the full comparison
            if (
                matcher.real_quick_ratio() >= self._similarity
                and matcher.quick_ratio() >= self._similarity
                and matcher.ratio() >= self._similarity
            ):
                return message.message_id

        return None

    def snapshot(self, key: Key) -> tuple[RecentMessage, ...]:
        """Return unexpired source messages suitable for persistence."""
        current = self._timer()

        # New messages refresh the cache entry, but older messages still expire alone
        return tuple(
            message
            for message in self._history.get(key, ())
            if 0 <= current - message.recorded_at < self._window
        )

    def restore(self, key: Key, messages: Iterable[RecentMessage]) -> None:
        """Restore source messages while retaining their original ages."""
        current = self._timer()

        # Deduplicate by message ID before keeping the newest source messages
        recent = {
            message.message_id: message
            for message in messages
            if 0 <= current - message.recorded_at < self._window
        }
        ordered = sorted(recent.values(), key=lambda message: message.recorded_at)

        if ordered:
            self._history[key] = deque(
                ordered[-self._history_size :], maxlen=self._history_size
            )
        else:
            self._history.pop(key, None)

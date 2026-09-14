from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from difflib import SequenceMatcher
from math import isfinite
from time import monotonic
from unicodedata import normalize

from cachetools import TTLCache


@dataclass(frozen=True, slots=True)
class _RecentMessage:
    content: str
    message_id: int
    recorded_at: float


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
        timer: Callable[[], float] = monotonic,
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
        self._history = TTLCache[Key, deque[_RecentMessage]](
            maxsize=maxsize, ttl=window, timer=timer
        )

    @staticmethod
    def _normalize(content: str) -> str:
        # Ignore case, punctuation and spacing changes when comparing reposts
        return re.sub(r'[\W_]+', ' ', normalize('NFKC', content).casefold()).strip()

    def record(self, key: Key, content: str, message_id: int) -> None:
        """Remember source text long enough to compare without storing a message object."""
        content = self._normalize(content)
        if len(content) < self._min_length:
            return

        messages = self._history.get(key)
        if messages is None:
            messages = deque[_RecentMessage](maxlen=self._history_size)
        messages.append(_RecentMessage(content, message_id, self._timer()))
        self._history[key] = messages

    def find_match(self, key: Key, content: str) -> int | None:
        """Return the newest similar source message ID, or None if none matches."""
        content = self._normalize(content)
        if len(content) < self._min_length:
            return None

        messages = self._history.get(key)
        if messages is None:
            return None

        current = self._timer()
        for message in reversed(messages):
            # New source messages refresh the cache entry but not older messages
            if current - message.recorded_at >= self._window:
                break
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

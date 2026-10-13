"""Own a guild's working counters and the decisions that update them."""

from __future__ import annotations

from hashlib import blake2b
from types import MappingProxyType
from typing import TYPE_CHECKING

from .bucket import BucketState, SavedBucket
from .constants import MAX_BUCKETS_PER_GUILD
from .types import SpamRule, SpamVerdict

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .types import MessageReference, SpamSettings, StateKey


class GuildSpamState:
    """Apply spam decisions to a private snapshot and track persistence changes.

    Each instance belongs to one transaction. Bucket snapshots and settings are
    immutable; only this working copy's mapping changes. Publish ``snapshot()``
    to the committed cache after the database transaction succeeds.
    """

    def __init__(
        self,
        *,
        settings: SpamSettings,
        buckets: Mapping[StateKey, SavedBucket],
        now: float,
    ) -> None:
        """Copy committed buckets and mark expired entries for deletion."""

        self._settings = settings
        self._now = now
        self._buckets = dict(buckets)
        self._changed = {
            key for key, entry in self._buckets.items() if entry.expires_at <= now
        }
        for key in self._changed:
            self._buckets.pop(key)

    @property
    def buckets(self) -> Mapping[StateKey, SavedBucket]:
        """Expose remaining rows without allowing untracked mutations."""

        return MappingProxyType(self._buckets)

    @property
    def changed(self) -> frozenset[StateKey]:
        """Return keys to upsert or delete when absent from the remaining rows."""

        return frozenset(self._changed)

    # Adapted from Rapptz/RoboDanny's cogs/mod.py SpamChecker
    # https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/mod.py

    def check(
        self,
        reference: MessageReference,
        content: str,
        *,
        is_new: bool,
    ) -> SpamVerdict | None:
        """Advance eligible counters and select the first violated rule.

        Parameters
        ----------
        reference : MessageReference
            Message identity and creation time, retained for burst cleanup.
        content : str
            Exact message text used by the shared repeated-content rule.
        is_new : bool
            Whether the author meets the account-age and membership-age limits.

        Returns
        -------
        SpamVerdict or None
            The first violated rule in new-user, user, then content order.
            Cleanup references include only the triggering author's messages.

        Notes
        -----
        Every eligible rule advances even when an earlier rule is violated.
        Replayed message IDs already retained in a bucket do not advance it again.
        """

        # Shared rules use channel keys; the user rule spans channels in one guild
        candidates: list[tuple[SpamRule, str]] = []
        if is_new:
            candidates.append((SpamRule.NEW_USER, str(reference.channel_id)))

        candidates.append((SpamRule.USER, str(reference.user_id)))

        # Empty attachment-only messages must not share one repeated-content bucket
        if content:
            digest = blake2b(content.encode(), digest_size=16).hexdigest()
            candidates.append((SpamRule.CONTENT, f'{reference.channel_id}:{digest}'))

        verdict: SpamVerdict | None = None

        for rule, key in candidates:
            rate = self._settings.rule(rule)
            if not rate.enabled:
                continue

            state_key = (rule.value, key)
            previous = self._buckets.get(state_key)
            bucket = (
                previous.bucket
                if previous is not None
                else BucketState(tat=reference.created_at)
            )
            result = bucket.advance(reference, rate)
            if result is None:
                continue

            updated, limited = result
            self._buckets[state_key] = updated
            self._changed.add(state_key)

            # Shared counters can include other authors, but cleanup must not do so
            if limited and verdict is None:
                verdict = SpamVerdict(
                    rule=rule,
                    count=rate.count,
                    period=rate.period,
                    messages=tuple(
                        ref
                        for ref in updated.bucket.messages
                        if ref.user_id == reference.user_id
                    ),
                )

        return verdict

    def reserve_action(self, user_id: int) -> bool:
        """Reserve an action cooldown if the member has no active reservation.

        Parameters
        ----------
        user_id : int
            Member whose moderation action needs a cooldown reservation.

        Returns
        -------
        bool
            Whether a new reservation was added to this working snapshot.
        """

        key = ('cooldown', str(user_id))
        if key in self._buckets:
            return False

        until = self._now + self._settings.action_cooldown
        self._buckets[key] = SavedBucket(
            bucket=BucketState(tat=until), expires_at=until
        )
        self._changed.add(key)
        return True

    def enforce_capacity(self) -> None:
        """Retain updated buckets and mark capacity evictions for deletion."""

        # Retain the most recently updated buckets at capacity
        for key in tuple(self._changed):
            if key in self._buckets:
                self._buckets[key] = self._buckets.pop(key)

        while len(self._buckets) > MAX_BUCKETS_PER_GUILD:
            key = next(iter(self._buckets))
            self._buckets.pop(key)
            self._changed.add(key)

    def snapshot(self) -> dict[StateKey, SavedBucket]:
        """Copy remaining rows for publication after a successful commit."""

        return dict(self._buckets)

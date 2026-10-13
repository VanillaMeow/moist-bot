"""Keep immutable rate debt and cleanup history beside their update logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, FiniteFloat

from .constants import MAX_BURST_MESSAGES

# Pydantic needs this field type at runtime to restore saved message references
from .types import MessageReference  # ruff: ignore[typing-only-first-party-import]

if TYPE_CHECKING:
    from .types import RateSettings


class BucketState(BaseModel):
    """Store rate-limit debt together with recent message references.

    Attributes
    ----------
    tat : float
        Theoretical arrival time as a Unix timestamp. For rate counters this is
        when accepted message debt finishes draining. Cooldown rows reuse it as
        the action cooldown's expiry time.
    messages : tuple[MessageReference, ...]
        Recent messages eligible for burst cleanup; empty for cooldown rows.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    tat: FiniteFloat
    messages: tuple[MessageReference, ...] = ()

    # Adapted from Rapptz/RoboDanny's cogs/mod.py RateLimit
    # https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/mod.py

    def advance(
        self, reference: MessageReference, rate: RateSettings
    ) -> tuple[SavedBucket, bool] | None:
        """Advance rate debt and retain bounded cleanup references.

        Parameters
        ----------
        reference : MessageReference
            Incoming message identity and creation time.
        rate : RateSettings
            Burst allowance and replenishment period for this counter.

        Returns
        -------
        tuple[SavedBucket, bool] or None
            Replacement snapshot and whether the allowance was exceeded, or
            ``None`` when the message ID is already retained.

        Notes
        -----
        Rejected messages leave rate debt unchanged but enter cleanup history.
        The current snapshot is never mutated.
        """

        # Replayed messages neither advance counters nor duplicate cleanup entries
        if any(ref.message_id == reference.message_id for ref in self.messages):
            return None

        now = reference.created_at

        # Match datetime's microsecond precision without float accumulation errors
        now_us = round(now * 1_000_000)
        drain_time_us = max(round(self.tat * 1_000_000), now_us)
        interval_us = round(rate.period / rate.count * 1_000_000)
        burst_allowance_us = round(rate.period * 1_000_000) - interval_us
        limited = drain_time_us - now_us > burst_allowance_us

        # Keep recent references even for rejected messages so the trigger is cleaned
        messages = tuple(
            ref for ref in self.messages if abs(now - ref.created_at) < rate.period
        )
        messages = (*messages, reference)[-MAX_BURST_MESSAGES:]

        bucket = BucketState(
            tat=(drain_time_us if limited else drain_time_us + interval_us) / 1_000_000,
            messages=messages,
        )

        # Cleanup history may outlive the rate debt and must survive restarts too
        updated = SavedBucket(
            bucket=bucket,
            expires_at=max(bucket.tat, now + rate.period),
        )

        return updated, limited


@dataclass(frozen=True, slots=True)
class SavedBucket:
    """Pair an immutable counter snapshot with its absolute expiry timestamp."""

    bucket: BucketState
    expires_at: float

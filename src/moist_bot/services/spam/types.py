"""Validated settings, message references, and moderation results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class SpamAction(StrEnum):
    """Supported responses to a committed spam detection."""

    ALERT = 'alert'
    TIMEOUT = 'timeout'
    BAN = 'ban'


class SpamRule(StrEnum):
    """Rule names exposed by commands and used by persistent counter keys."""

    USER = 'user'
    CONTENT = 'content'
    NEW_USER = 'new-user'


class ExemptionKind(StrEnum):
    """Kinds of Discord entities that can bypass spam checks."""

    USER = 'user'
    ROLE = 'role'
    CHANNEL = 'channel'


class RateSettings(BaseModel):
    """Configure one rule's burst allowance and replenishment period.

    Attributes
    ----------
    enabled : bool
        Whether messages contribute to this rule's counters.
    count : int
        Number of messages accepted in a simultaneous burst.
    period : float
        Seconds required to replenish the entire allowance.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    enabled: bool = True
    count: int = Field(ge=1, le=1000)
    period: FiniteFloat = Field(ge=0.001, le=3600)


class SpamSettings(BaseModel):
    """Validate a guild's rule settings and enforcement options.

    Durations are in seconds. Instances are immutable so configuration readers
    cannot change the cached snapshot before a database transaction commits.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    action: SpamAction = SpamAction.TIMEOUT
    timeout_seconds: int = Field(default=600, ge=1, le=28 * 86400)
    delete_burst: bool = True
    action_cooldown: int = Field(default=60, ge=1, le=3600)
    log_channel_id: int | None = Field(default=None, gt=0)

    # Defaults match the message-based rules retained from RoboDanny
    user: RateSettings = Field(
        default_factory=lambda: RateSettings(count=10, period=12)
    )
    content: RateSettings = Field(
        default_factory=lambda: RateSettings(count=5, period=15)
    )
    new_user: RateSettings = Field(
        default_factory=lambda: RateSettings(count=30, period=35)
    )

    def rule(self, rule: SpamRule) -> RateSettings:
        """Return the settings for a command-facing rule name.

        Parameters
        ----------
        rule : SpamRule
            Rule whose nested configuration is needed.

        Returns
        -------
        RateSettings
            The immutable configuration for that rule.
        """

        match rule:
            case SpamRule.USER:
                return self.user
            case SpamRule.CONTENT:
                return self.content
            case SpamRule.NEW_USER:
                return self.new_user


class MessageReference(BaseModel):
    """Retain a message's identity and Unix timestamp for targeted burst cleanup."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    message_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    created_at: FiniteFloat


# Keys pair a rule name or 'cooldown' with its user, channel, or content identity
type StateKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class GuildSpamSettings:
    """Expose one committed guild configuration and its explicit exemptions."""

    enabled: bool
    settings: SpamSettings
    exemptions: frozenset[tuple[ExemptionKind, int]] = frozenset()


@dataclass(frozen=True, slots=True)
class SpamVerdict:
    """Describe the violated rule and the triggering member's cleanup references."""

    rule: SpamRule
    count: int
    period: float
    messages: tuple[MessageReference, ...]

    @property
    def reason(self) -> str:
        """Format the violated allowance for incident logs and Discord audit reasons."""

        return f'Spam: {self.rule.value}, allowance {self.count}/{self.period:g}s'


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """Keep punishment success separate from cleanup counts and cleanup failures."""

    succeeded: bool
    error: str | None = None
    deleted: int = 0
    cleanup_error: str | None = None

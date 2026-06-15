from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:

    class GuildMessage(discord.Message):
        """A message in a guild."""

        guild: discord.Guild
        author: discord.Member  # type: ignore[reportIncompatibleVariableOverride]
else:
    GuildMessage = discord.Message


class HoneypotPunishmentAction(StrEnum):
    SOFTBAN = auto()
    BAN = auto()


@dataclass(slots=True)
class HoneypotScanBatch:
    """Messages found for one member during a honeypot scan."""

    member: discord.Member
    messages: list[GuildMessage]


@dataclass(frozen=True, slots=True)
class HoneypotScanBatchResult:
    """Result from handling one scanned member batch."""

    messages_deleted: int = 0
    incident_recorded: bool = False


@dataclass(frozen=True, slots=True)
class Punishment:
    """Outcome of an automatic honeypot punishment."""

    action: HoneypotPunishmentAction
    trigger_count: int
    delete_message_seconds: int
    succeeded: bool
    error: str | None
    ban_applied: bool


@dataclass(slots=True)
class HoneypotScanResult:
    """Summary of a honeypot scan."""

    configs_checked: int = 0
    messages_found: int = 0
    members_handled: int = 0
    incidents_recorded: int = 0
    messages_deleted: int = 0

    def merge(self, other: HoneypotScanResult) -> None:
        """Add another scan result into this one."""

        self.configs_checked += other.configs_checked
        self.messages_found += other.messages_found
        self.members_handled += other.members_handled
        self.incidents_recorded += other.incidents_recorded
        self.messages_deleted += other.messages_deleted


class HoneypotScanAlreadyRunningError(Exception):
    """Raised when a guild already has an active honeypot scan."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id: int = guild_id
        super().__init__(f'Honeypot scan already running for guild {guild_id}.')

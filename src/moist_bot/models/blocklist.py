# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar, Final

from sqlalchemy import BigInteger, Column, DateTime, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

BLOCKLIST_SENTINEL_ID: Final = 0


class BlocklistScope(StrEnum):
    """Blocklist entry scopes."""

    GLOBAL_USER = 'global_user'
    GUILD_USER = 'guild_user'
    GUILD = 'guild'


class BlocklistSource(StrEnum):
    """Blocklist entry sources."""

    MANUAL = 'manual'
    AUTO = 'auto'


class ChannelPolicyMode(StrEnum):
    """Guild channel policy modes."""

    OFF = 'off'
    DENYLIST = 'denylist'
    ALLOWLIST = 'allowlist'


class BlocklistEntry(SQLModel, table=True):
    """Persistent blocklist entry."""

    __tablename__: ClassVar[str] = 'blocklist_entries'
    __table_args__: ClassVar = (
        UniqueConstraint(
            'scope',
            'guild_id',
            'user_id',
            name='uq_blocklist_entries_scope_key',
        ),
        Index('ix_blocklist_entries_scope_guild', 'scope', 'guild_id'),
        Index('ix_blocklist_entries_scope_user', 'scope', 'user_id'),
    )

    id: int | None = Field(default=None, primary_key=True)
    scope: str = Field(max_length=30, index=True)
    guild_id: int = Field(default=BLOCKLIST_SENTINEL_ID, sa_type=BigInteger, index=True)
    user_id: int = Field(default=BLOCKLIST_SENTINEL_ID, sa_type=BigInteger, index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    created_by_id: int | None = Field(default=None, sa_type=BigInteger, index=True)
    source: str = Field(default=BlocklistSource.MANUAL, max_length=30, index=True)
    reason: str | None = Field(default=None, max_length=500)


class GuildChannelPolicy(SQLModel, table=True):
    """Per-guild command channel policy."""

    __tablename__: ClassVar[str] = 'guild_channel_policies'

    guild_id: int = Field(sa_type=BigInteger, primary_key=True)
    mode: str = Field(default=ChannelPolicyMode.OFF, max_length=30)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_by_id: int | None = Field(default=None, sa_type=BigInteger)


class GuildChannelPolicyChannel(SQLModel, table=True):
    """Channel id attached to a guild channel policy."""

    __tablename__: ClassVar[str] = 'guild_channel_policy_channels'
    __table_args__: ClassVar = (
        UniqueConstraint(
            'guild_id',
            'channel_id',
            name='uq_guild_channel_policy_channels_guild_channel',
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int = Field(sa_type=BigInteger, index=True)
    channel_id: int = Field(sa_type=BigInteger, index=True)

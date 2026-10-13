# pyright: reportIncompatibleVariableOverride=false

"""Database rows for guild configuration, explicit exemptions, and spam incidents."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import BigInteger, Column, DateTime, Index
from sqlmodel import Field, SQLModel


class GuildSpamConfig(SQLModel, table=True):
    """Persist one guild's enabled state and validated settings as JSON.

    ``SpamConfig`` validates the JSON through ``SpamSettings`` when loading it.
    Update metadata records who last changed either settings or exemptions.
    """

    __tablename__: ClassVar[str] = 'guild_spam_configs'

    guild_id: int = Field(sa_type=BigInteger, primary_key=True)
    enabled: bool = False
    settings_json: str

    # Settings and exemption changes share the same audit metadata
    updated_by_id: int = Field(sa_type=BigInteger)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class GuildSpamExemption(SQLModel, table=True):
    """Identify an exempt user, role, or channel within one guild.

    The composite primary key prevents duplicate exemptions for the same entity.
    """

    __tablename__: ClassVar[str] = 'guild_spam_exemptions'

    guild_id: int = Field(sa_type=BigInteger, primary_key=True)
    kind: str = Field(primary_key=True, max_length=20)
    entity_id: int = Field(sa_type=BigInteger, primary_key=True)


class SpamIncident(SQLModel, table=True):
    """Record a detection before enforcement and retain its eventual outcome.

    ``status`` describes punishment. Cleanup counts and errors are separate so
    failed message deletion cannot hide a successfully applied punishment.
    Incident history survives removal of the guild's active configuration.
    """

    __tablename__: ClassVar[str] = 'spam_incidents'
    __table_args__: ClassVar = (
        Index('ix_spam_incidents_guild_triggered', 'guild_id', 'triggered_at'),
    )

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int = Field(sa_type=BigInteger)
    user_id: int = Field(sa_type=BigInteger)
    channel_id: int = Field(sa_type=BigInteger)
    message_id: int = Field(sa_type=BigInteger, unique=True)

    # Preserve the detection evidence and requested action for moderator review
    rule: str
    action: str
    reason: str
    content_excerpt: str

    # Pending rows are reconciled as interrupted if a restart prevents finalization
    status: str = 'pending'
    error: str | None = None

    # Cleanup failures do not change the punishment's recorded status
    messages_deleted: int = 0
    cleanup_error: str | None = None

    triggered_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

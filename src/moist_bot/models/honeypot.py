# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
)
from sqlmodel import Field, SQLModel, col, select

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


class GuildHoneypotConfig(SQLModel, table=True):
    """Per-guild honeypot configuration."""

    __tablename__: ClassVar[str] = 'guild_honeypot_configs'
    __table_args__: ClassVar = (
        UniqueConstraint('guild_id', name='uq_guild_honeypot_configs_guild_id'),
    )

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int = Field(sa_type=BigInteger, index=True)
    channel_id: int = Field(sa_type=BigInteger, index=True)
    log_channel_id: int = Field(sa_type=BigInteger, index=True)
    enabled: bool = Field(default=True, index=True)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_by_id: int | None = Field(default=None, sa_type=BigInteger)


class HoneypotIncident(SQLModel, table=True):
    """Audit record for one honeypot trigger."""

    __tablename__: ClassVar[str] = 'honeypot_incidents'
    __table_args__: ClassVar = (
        Index(
            'ix_honeypot_incidents_guild_triggered',
            'guild_id',
            'triggered_at',
            'id',
        ),
        Index(
            'ix_honeypot_incidents_guild_user_triggered',
            'guild_id',
            'user_id',
            'triggered_at',
            'id',
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    config_id: int | None = Field(
        sa_column=Column(
            ForeignKey('guild_honeypot_configs.id', ondelete='SET NULL'),
            nullable=True,
            index=True,
        )
    )
    guild_id: int = Field(sa_type=BigInteger, index=True)
    channel_id: int = Field(sa_type=BigInteger, index=True)
    log_channel_id: int = Field(sa_type=BigInteger, index=True)
    user_id: int = Field(sa_type=BigInteger, index=True)
    message_id: int = Field(sa_type=BigInteger, index=True)
    message_created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    triggered_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    content_excerpt: str | None = Field(default=None, max_length=500)
    attachment_count: int = Field(default=0)
    trigger_count: int = Field(default=1)
    delete_message_seconds: int = Field(default=0)
    softbanned: bool = Field(default=False, index=True)
    softban_error: str | None = Field(default=None, max_length=500)
    log_sent: bool = Field(default=False, index=True)
    log_error: str | None = Field(default=None, max_length=500)

    @classmethod
    async def history(
        cls,
        session: AsyncSession,
        *,
        limit: int | None,
        offset: int = 0,
        criteria: Iterable[ColumnElement[bool]] = (),
    ) -> list[HoneypotIncident]:
        """Return incident events in newest-first order."""

        statement = (
            select(cls)
            .where(*criteria)
            .order_by(col(cls.triggered_at).desc(), col(cls.id).desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        if offset > 0:
            statement = statement.offset(offset)

        result = await session.execute(statement)
        return list(result.scalars().all())

    @classmethod
    async def history_count(
        cls,
        session: AsyncSession,
        *,
        criteria: Iterable[ColumnElement[bool]] = (),
    ) -> int:
        """Return the number of incidents matching the criteria."""

        result = await session.execute(select(func.count(col(cls.id))).where(*criteria))
        return result.scalar_one()

    @classmethod
    async def trigger_count_for_user(
        cls,
        session: AsyncSession,
        *,
        guild_id: int,
        user_id: int,
    ) -> int:
        """Return the number of incidents recorded for a guild member."""

        result = await session.execute(
            select(func.count(col(cls.id))).where(
                col(cls.guild_id) == guild_id,
                col(cls.user_id) == user_id,
            )
        )
        return result.scalar_one()

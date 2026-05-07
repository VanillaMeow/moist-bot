# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import ClassVar, NamedTuple, cast

from sqlalchemy import BigInteger, Column, DateTime, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Field, SQLModel, col, select


class CountAndFirst(NamedTuple):
    total: int
    first_used: datetime | None


class LabelCount(NamedTuple):
    label: str
    uses: int


class UserCount(NamedTuple):
    author_id: int
    uses: int


class GuildCount(NamedTuple):
    guild_id: int | None
    uses: int


class FailedCount(NamedTuple):
    failed: bool
    uses: int


class CommandUsage(SQLModel, table=True):
    __tablename__: ClassVar[str] = 'command_usage'

    id: int | None = Field(default=None, primary_key=True)
    guild_id: int | None = Field(
        default=None,
        sa_type=BigInteger,
        index=True,
    )
    channel_id: int = Field(sa_type=BigInteger, index=True)
    author_id: int = Field(sa_type=BigInteger, index=True)
    used_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    prefix: str = Field(max_length=100)
    command: str = Field(max_length=200, index=True)
    failed: bool = Field(default=False, index=True)
    app_command: bool = Field(default=False, index=True)

    @staticmethod
    def normalize_datetime(value: datetime | str | None) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)

        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @classmethod
    async def count_and_first(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
    ) -> CountAndFirst:
        result = await session.execute(
            select(
                func.count(col(cls.id)).label('total'),
                func.min(col(cls.used_at)).label('first_used'),
            ).where(*criteria)
        )
        row = result.mappings().one()
        return CountAndFirst(
            total=int(row['total']),
            first_used=cls.normalize_datetime(row['first_used']),
        )

    @classmethod
    async def top_commands(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
        limit: int = 5,
    ) -> list[LabelCount]:
        command = cast('ColumnElement[str]', col(cls.command))
        uses = func.count(col(cls.id)).label('uses')
        result = await session.execute(
            select(command.label('label'), uses)
            .where(*criteria)
            .group_by(command)
            .order_by(uses.desc())
            .limit(limit)
        )
        return [
            LabelCount(label=str(row['label']), uses=int(row['uses']))
            for row in result.mappings().all()
        ]

    @classmethod
    async def top_users(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
        limit: int = 5,
    ) -> list[UserCount]:
        author_id = col(cls.author_id)
        uses = func.count(col(cls.id)).label('uses')
        result = await session.execute(
            select(author_id, uses)
            .where(*criteria)
            .group_by(author_id)
            .order_by(uses.desc())
            .limit(limit)
        )
        return [
            UserCount(author_id=int(row['author_id']), uses=int(row['uses']))
            for row in result.mappings().all()
        ]

    @classmethod
    async def top_guilds(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
        limit: int = 5,
    ) -> list[GuildCount]:
        guild_id = col(cls.guild_id)
        uses = func.count(col(cls.id)).label('uses')
        result = await session.execute(
            select(guild_id, uses)
            .where(*criteria)
            .group_by(guild_id)
            .order_by(uses.desc())
            .limit(limit)
        )
        return [
            GuildCount(
                guild_id=None if row['guild_id'] is None else int(row['guild_id']),
                uses=int(row['uses']),
            )
            for row in result.mappings().all()
        ]

    @classmethod
    async def failed_counts(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
    ) -> list[FailedCount]:
        uses = func.count(col(cls.id)).label('uses')
        result = await session.execute(
            select(col(cls.failed), uses).where(*criteria).group_by(col(cls.failed))
        )
        return [
            FailedCount(failed=bool(row['failed']), uses=int(row['uses']))
            for row in result.mappings().all()
        ]

    @classmethod
    async def history(
        cls,
        session: AsyncSession,
        *,
        limit: int,
        criteria: Iterable[ColumnElement[bool]] = (),
    ) -> list[CommandUsage]:
        result = await session.execute(
            select(cls).where(*criteria).order_by(col(cls.used_at).desc()).limit(limit)
        )
        return list(result.scalars().all())

# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Final, cast

from pydantic import field_validator
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, SQLModel, col, select

from moist_bot.utils.converters import normalize_datetime

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

# Rollup keys use sentinels because SQL unique constraints do not collapse NULLs
STATS_SENTINEL_ID: Final = 0
STATS_SENTINEL_COMMAND: Final = ''


class CommandStatsScope(StrEnum):
    """Rollup dimensions stored in command usage stats."""

    GLOBAL = 'global'
    GLOBAL_COMMAND = 'global_command'
    GLOBAL_GUILD = 'global_guild'
    GLOBAL_USER = 'global_user'
    GUILD = 'guild'
    GUILD_COMMAND = 'guild_command'
    GUILD_USER = 'guild_user'
    GUILD_USER_COMMAND = 'guild_user_command'


type CommandUsageStatsKey = tuple[CommandStatsScope, int, int, str]


class CommandUsageSummary(SQLModel):
    """Total usage count with the earliest matching command timestamp."""

    total_uses: int = 0
    first_used: datetime | None

    @field_validator('first_used', mode='before')
    @classmethod
    def normalize_first_used(cls, value: datetime | str | None) -> datetime | None:
        """Normalize SQLite datetime values before validation."""

        return normalize_datetime(value)


class CommandUsageCommandCount(SQLModel):
    """Command usage count projected from an aggregate query."""

    label: str
    uses: int


class CommandUsageUserCount(SQLModel):
    """User usage count projected from an aggregate query."""

    author_id: int
    uses: int


class CommandUsageGuildCount(SQLModel):
    """Guild usage count projected from an aggregate query."""

    guild_id: int | None
    uses: int

    @field_validator('guild_id', mode='before')
    @classmethod
    def normalize_sentinel_id(cls, value: int | None) -> int | None:
        """Convert the stats-table sentinel back to the public DM shape."""

        return None if value == STATS_SENTINEL_ID else value


class CommandUsageFailureCount(SQLModel):
    """Success or failure usage count projected from an aggregate query."""

    failed: bool
    uses: int


class CommandUsage(SQLModel, table=True):
    """Raw command invocation used for history and time-windowed stats."""

    __tablename__: ClassVar[str] = 'command_usage'
    __table_args__: ClassVar = (
        Index(
            'ix_command_usage_guild_author_used_at',
            'guild_id',
            'author_id',
            'used_at',
        ),
        Index(
            'ix_command_usage_guild_command_used_at',
            'guild_id',
            'command',
            'used_at',
        ),
        Index(
            'ix_command_usage_guild_used_at_author',
            'guild_id',
            'used_at',
            'author_id',
        ),
        Index(
            'ix_command_usage_used_at_command',
            'used_at',
            'command',
        ),
    )

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

    @classmethod
    async def count_and_first(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
    ) -> CommandUsageSummary:
        """Count matching raw events and return the first matching timestamp."""

        result = await session.execute(
            select(
                func.count(col(cls.id)).label('total_uses'),
                func.min(col(cls.used_at)).label('first_used'),
            ).where(*criteria)
        )

        row = result.mappings().one()
        return CommandUsageSummary.model_validate(row)

    @classmethod
    async def top_commands(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
        limit: int = 5,
    ) -> list[CommandUsageCommandCount]:
        """Return the most used commands from raw events."""

        command = cast('ColumnElement[str]', col(cls.command))
        uses = func.count(col(cls.id)).label('uses')

        result = await session.execute(
            select(command.label('label'), uses)
            .where(*criteria)
            .group_by(command)
            .order_by(uses.desc(), command.asc())
            .limit(limit)
        )

        return [
            CommandUsageCommandCount.model_validate(row)
            for row in result.mappings().all()
        ]

    @classmethod
    async def top_users(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
        limit: int = 5,
    ) -> list[CommandUsageUserCount]:
        """Return the most active users from raw events."""

        author_id = cast('ColumnElement[int]', col(cls.author_id))
        uses = func.count(col(cls.id)).label('uses')

        result = await session.execute(
            select(author_id, uses)
            .where(*criteria)
            .group_by(author_id)
            .order_by(uses.desc(), author_id.asc())
            .limit(limit)
        )

        return [
            CommandUsageUserCount.model_validate(row) for row in result.mappings().all()
        ]

    @classmethod
    async def top_guilds(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
        limit: int = 5,
    ) -> list[CommandUsageGuildCount]:
        """Return the most active guilds from raw events."""

        guild_id = cast('ColumnElement[int | None]', col(cls.guild_id))
        uses = func.count(col(cls.id)).label('uses')

        result = await session.execute(
            select(guild_id, uses)
            .where(*criteria)
            .group_by(guild_id)
            .order_by(uses.desc(), guild_id.asc())
            .limit(limit)
        )

        return [
            CommandUsageGuildCount.model_validate(row)
            for row in result.mappings().all()
        ]

    @classmethod
    async def failed_counts(
        cls,
        session: AsyncSession,
        *criteria: ColumnElement[bool],
    ) -> list[CommandUsageFailureCount]:
        """Return success and failure counts from raw events."""

        uses = func.count(col(cls.id)).label('uses')

        result = await session.execute(
            select(col(cls.failed), uses).where(*criteria).group_by(col(cls.failed))
        )

        return [
            CommandUsageFailureCount.model_validate(row)
            for row in result.mappings().all()
        ]

    @classmethod
    async def history(
        cls,
        session: AsyncSession,
        *,
        limit: int | None,
        offset: int = 0,
        criteria: Iterable[ColumnElement[bool]] = (),
    ) -> list[CommandUsage]:
        """Return raw command events in newest-first order."""

        statement = (
            select(cls)
            .where(*criteria)
            .order_by(col(cls.used_at).desc(), col(cls.id).desc())
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
        """Return the number of raw command events matching the criteria."""

        result = await session.execute(select(func.count(col(cls.id))).where(*criteria))
        return result.scalar_one()


class CommandUsageStats(SQLModel, table=True):
    """Precomputed all-time command counters keyed by scope and dimensions."""

    __tablename__: ClassVar[str] = 'command_usage_stats'
    __table_args__: ClassVar = (
        # One logical counter per scope and dimension tuple
        UniqueConstraint(
            'scope',
            'guild_id',
            'author_id',
            'command',
            name='uq_command_usage_stats_scope_key',
        ),
        Index(
            'ix_command_usage_stats_scope_total',
            'scope',
            'total_uses',
        ),
        Index(
            'ix_command_usage_stats_scope_guild_total',
            'scope',
            'guild_id',
            'total_uses',
        ),
        Index(
            'ix_command_usage_stats_scope_author_total',
            'scope',
            'author_id',
            'total_uses',
        ),
        Index(
            'ix_command_usage_stats_scope_guild_author_total',
            'scope',
            'guild_id',
            'author_id',
            'total_uses',
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    scope: str = Field(max_length=50)
    guild_id: int = Field(default=STATS_SENTINEL_ID, sa_type=BigInteger)
    author_id: int = Field(default=STATS_SENTINEL_ID, sa_type=BigInteger)
    command: str = Field(default=STATS_SENTINEL_COMMAND, max_length=200)
    total_uses: int = Field(default=0)
    failed_uses: int = Field(default=0)
    app_command_uses: int = Field(default=0)
    first_used: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    last_used: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )

    @staticmethod
    def _usage_keys(usage: CommandUsage) -> Sequence[CommandUsageStatsKey]:
        """Return every rollup key touched by a raw usage event."""

        guild_id = (
            usage.guild_id if usage.guild_id is not None else STATS_SENTINEL_ID
        )
        global_keys: list[CommandUsageStatsKey] = [
            (
                CommandStatsScope.GLOBAL,
                STATS_SENTINEL_ID,
                STATS_SENTINEL_ID,
                STATS_SENTINEL_COMMAND,
            ),
            (
                CommandStatsScope.GLOBAL_COMMAND,
                STATS_SENTINEL_ID,
                STATS_SENTINEL_ID,
                usage.command,
            ),
            (
                CommandStatsScope.GLOBAL_GUILD,
                guild_id,
                STATS_SENTINEL_ID,
                STATS_SENTINEL_COMMAND,
            ),
            (
                CommandStatsScope.GLOBAL_USER,
                STATS_SENTINEL_ID,
                usage.author_id,
                STATS_SENTINEL_COMMAND,
            ),
        ]
        if usage.guild_id is None:
            # DM usage contributes to global scopes only
            return global_keys

        # Guild usage fans out into guild, user, command, and user-command scopes
        return [
            *global_keys,
            (
                CommandStatsScope.GUILD,
                guild_id,
                STATS_SENTINEL_ID,
                STATS_SENTINEL_COMMAND,
            ),
            (
                CommandStatsScope.GUILD_COMMAND,
                guild_id,
                STATS_SENTINEL_ID,
                usage.command,
            ),
            (
                CommandStatsScope.GUILD_USER,
                guild_id,
                usage.author_id,
                STATS_SENTINEL_COMMAND,
            ),
            (
                CommandStatsScope.GUILD_USER_COMMAND,
                guild_id,
                usage.author_id,
                usage.command,
            ),
        ]

    @classmethod
    def _stats_for_usage(
        cls, usages: Iterable[CommandUsage]
    ) -> list[CommandUsageStats]:
        """Accumulate raw usage events into stats rows for one write batch."""

        stats_by_key: dict[CommandUsageStatsKey, CommandUsageStats] = {}

        for usage in usages:
            for key in cls._usage_keys(usage):
                stats = stats_by_key.get(key)

                # If the stats row does not exist, create it
                if stats is None:
                    scope, guild_id, author_id, command = key
                    stats = cls(
                        scope=scope.value,
                        guild_id=guild_id,
                        author_id=author_id,
                        command=command,
                        total_uses=0,
                        failed_uses=0,
                        app_command_uses=0,
                        first_used=usage.used_at,
                        last_used=usage.used_at,
                    )
                    stats_by_key[key] = stats

                # Increment the stats row
                stats.total_uses += 1
                stats.failed_uses += int(usage.failed)
                stats.app_command_uses += int(usage.app_command)
                stats.first_used = min(stats.first_used, usage.used_at)
                stats.last_used = max(stats.last_used, usage.used_at)

        return list(stats_by_key.values())

    @classmethod
    async def upsert_usage_batch(
        cls,
        session: AsyncSession,
        usages: Iterable[CommandUsage],
    ) -> None:
        """Insert or increment precomputed stats for a raw usage batch."""

        stats = cls._stats_for_usage(usages)
        if not stats:
            return

        rows = [row.model_dump(exclude={'id'}) for row in stats]
        table = cast('Table', cls.__table__)  # type: ignore[reportAttributeAccessIssue]
        statement = sqlite_insert(table).values(rows)

        # `excluded` contains the values from the insert row that hit the conflict
        excluded = statement.excluded
        columns = table.c

        # The unique rollup key turns repeated inserts into atomic counter updates
        statement = statement.on_conflict_do_update(
            index_elements=['scope', 'guild_id', 'author_id', 'command'],
            set_={
                'total_uses': columns['total_uses'] + excluded.total_uses,
                'failed_uses': columns['failed_uses'] + excluded.failed_uses,
                'app_command_uses': (
                    columns['app_command_uses'] + excluded.app_command_uses
                ),
                'first_used': func.min(columns['first_used'], excluded.first_used),
                'last_used': func.max(columns['last_used'], excluded.last_used),
            },
        )
        await session.execute(statement)

    @classmethod
    async def count_and_first(
        cls,
        session: AsyncSession,
        scope: CommandStatsScope,
        *,
        guild_id: int = STATS_SENTINEL_ID,
        author_id: int = STATS_SENTINEL_ID,
        command: str = STATS_SENTINEL_COMMAND,
    ) -> CommandUsageSummary:
        """Return a precomputed total and first-used timestamp."""

        total_uses = cast('ColumnElement[int]', col(cls.total_uses))
        first_used = cast('ColumnElement[datetime]', col(cls.first_used))

        result = await session.execute(
            select(
                total_uses.label('total_uses'),
                first_used.label('first_used'),
            ).where(
                col(cls.scope) == scope.value,
                col(cls.guild_id) == guild_id,
                col(cls.author_id) == author_id,
                col(cls.command) == command,
            )
        )

        row = result.mappings().one_or_none()
        if row is None:
            return CommandUsageSummary(total_uses=0, first_used=None)

        return CommandUsageSummary.model_validate(row)

    @classmethod
    async def top_commands(
        cls,
        session: AsyncSession,
        scope: CommandStatsScope,
        *,
        guild_id: int = STATS_SENTINEL_ID,
        author_id: int = STATS_SENTINEL_ID,
        limit: int = 5,
    ) -> list[CommandUsageCommandCount]:
        """Return the most used commands from precomputed stats."""

        command = cast('ColumnElement[str]', col(cls.command))
        total_uses = cast('ColumnElement[int]', col(cls.total_uses))

        result = await session.execute(
            select(
                command.label('label'),
                total_uses.label('uses'),
            )
            .where(
                col(cls.scope) == scope.value,
                col(cls.guild_id) == guild_id,
                col(cls.author_id) == author_id,
            )
            .order_by(total_uses.desc(), command.asc())
            .limit(limit)
        )

        return [
            CommandUsageCommandCount.model_validate(row)
            for row in result.mappings().all()
        ]

    @classmethod
    async def top_users(
        cls,
        session: AsyncSession,
        scope: CommandStatsScope,
        *,
        guild_id: int = STATS_SENTINEL_ID,
        limit: int = 5,
    ) -> list[CommandUsageUserCount]:
        """Return the most active users from precomputed stats."""

        author_id = cast('ColumnElement[int]', col(cls.author_id))
        total_uses = cast('ColumnElement[int]', col(cls.total_uses))

        result = await session.execute(
            select(
                author_id.label('author_id'),
                total_uses.label('uses'),
            )
            .where(
                col(cls.scope) == scope.value,
                col(cls.guild_id) == guild_id,
            )
            .order_by(total_uses.desc(), author_id.asc())
            .limit(limit)
        )

        return [
            CommandUsageUserCount.model_validate(row) for row in result.mappings().all()
        ]

    @classmethod
    async def top_guilds(
        cls,
        session: AsyncSession,
        *,
        limit: int = 5,
    ) -> list[CommandUsageGuildCount]:
        """Return the most active guilds from precomputed stats."""

        guild_id = cast('ColumnElement[int]', col(cls.guild_id))
        total_uses = cast('ColumnElement[int]', col(cls.total_uses))

        result = await session.execute(
            select(
                guild_id.label('guild_id'),
                total_uses.label('uses'),
            )
            .where(col(cls.scope) == CommandStatsScope.GLOBAL_GUILD.value)
            .order_by(total_uses.desc(), guild_id.asc())
            .limit(limit)
        )

        return [
            CommandUsageGuildCount.model_validate(row)
            for row in result.mappings().all()
        ]

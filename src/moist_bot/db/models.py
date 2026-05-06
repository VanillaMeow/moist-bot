# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import BigInteger, Column, DateTime
from sqlmodel import Field, SQLModel


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

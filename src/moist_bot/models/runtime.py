# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar, Final

from sqlalchemy import BigInteger, Column, DateTime
from sqlmodel import Field, SQLModel

RESTART_NOTICE_ID: Final = 1


class RestartNotice(SQLModel, table=True):
    """Message to update after a process restart."""

    __tablename__: ClassVar[str] = 'restart_notices'

    id: int = Field(default=RESTART_NOTICE_ID, primary_key=True)
    guild_id: int | None = Field(default=None, sa_type=BigInteger)
    channel_id: int = Field(sa_type=BigInteger)
    message_id: int = Field(sa_type=BigInteger)
    requested_by_id: int = Field(sa_type=BigInteger)
    requested_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

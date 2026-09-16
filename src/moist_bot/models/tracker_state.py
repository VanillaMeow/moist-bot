# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from typing import ClassVar

from sqlmodel import Field, SQLModel


class TrackerState(SQLModel, table=True):
    """Persist one tracker key independently of the bot process."""

    __tablename__: ClassVar[str] = 'tracker_states'

    namespace: str = Field(primary_key=True)
    tracker: str = Field(primary_key=True)
    key: str = Field(primary_key=True)
    payload: str
    expires_at: float = Field(index=True)

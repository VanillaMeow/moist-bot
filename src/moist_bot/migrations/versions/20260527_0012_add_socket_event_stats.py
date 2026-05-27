from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260527_0012'
down_revision: str | None = '20260527_0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'socket_event_stats',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('total_events', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_type', name='uq_socket_event_stats_event_type'),
    )


def downgrade() -> None:
    op.drop_table('socket_event_stats')

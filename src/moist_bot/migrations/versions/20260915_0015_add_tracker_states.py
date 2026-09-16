from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260915_0015'
down_revision: str | None = '20260529_0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'tracker_states',
        sa.Column('namespace', sa.String(), nullable=False),
        sa.Column('tracker', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('payload', sa.String(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('namespace', 'tracker', 'key'),
    )
    op.create_index('ix_tracker_states_expires_at', 'tracker_states', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_tracker_states_expires_at', table_name='tracker_states')
    op.drop_table('tracker_states')

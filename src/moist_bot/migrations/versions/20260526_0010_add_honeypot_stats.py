from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260526_0010'
down_revision: str | None = '20260526_0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'honeypot_guild_stats',
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('total_incidents', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('guild_id'),
    )
    op.create_table(
        'honeypot_user_stats',
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('total_incidents', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('guild_id', 'user_id'),
    )

    op.execute(
        """
        INSERT INTO honeypot_guild_stats (guild_id, total_incidents)
        SELECT guild_id, COUNT(id)
        FROM honeypot_incidents
        GROUP BY guild_id
        """
    )
    op.execute(
        """
        INSERT INTO honeypot_user_stats (guild_id, user_id, total_incidents)
        SELECT guild_id, user_id, COUNT(id)
        FROM honeypot_incidents
        GROUP BY guild_id, user_id
        """
    )


def downgrade() -> None:
    op.drop_table('honeypot_user_stats')
    op.drop_table('honeypot_guild_stats')

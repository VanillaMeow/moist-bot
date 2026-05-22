from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260521_0005'
down_revision: str | None = '20260518_0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'guild_channel_policy_permissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('permission_name', sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'guild_id',
            'permission_name',
            name='uq_guild_channel_policy_permissions_guild_permission',
        ),
    )
    op.create_index(
        'ix_guild_channel_policy_permissions_guild_id',
        'guild_channel_policy_permissions',
        ['guild_id'],
    )
    op.create_index(
        'ix_guild_channel_policy_permissions_permission_name',
        'guild_channel_policy_permissions',
        ['permission_name'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_guild_channel_policy_permissions_permission_name',
        table_name='guild_channel_policy_permissions',
    )
    op.drop_index(
        'ix_guild_channel_policy_permissions_guild_id',
        table_name='guild_channel_policy_permissions',
    )
    op.drop_table('guild_channel_policy_permissions')

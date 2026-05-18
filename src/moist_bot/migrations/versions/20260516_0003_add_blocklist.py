from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260516_0003'
down_revision: str | None = '20260509_0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'blocklist_entries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=30), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by_id', sa.BigInteger(), nullable=True),
        sa.Column('source', sa.String(length=30), nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'scope',
            'guild_id',
            'user_id',
            name='uq_blocklist_entries_scope_key',
        ),
    )
    op.create_index('ix_blocklist_entries_scope', 'blocklist_entries', ['scope'])
    op.create_index('ix_blocklist_entries_guild_id', 'blocklist_entries', ['guild_id'])
    op.create_index('ix_blocklist_entries_user_id', 'blocklist_entries', ['user_id'])
    op.create_index(
        'ix_blocklist_entries_created_at',
        'blocklist_entries',
        ['created_at'],
    )
    op.create_index(
        'ix_blocklist_entries_created_by_id',
        'blocklist_entries',
        ['created_by_id'],
    )
    op.create_index('ix_blocklist_entries_source', 'blocklist_entries', ['source'])
    op.create_index(
        'ix_blocklist_entries_scope_guild',
        'blocklist_entries',
        ['scope', 'guild_id'],
    )
    op.create_index(
        'ix_blocklist_entries_scope_user',
        'blocklist_entries',
        ['scope', 'user_id'],
    )

    op.create_table(
        'guild_channel_policies',
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('mode', sa.String(length=30), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by_id', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('guild_id'),
    )

    op.create_table(
        'guild_channel_policy_channels',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'guild_id',
            'channel_id',
            name='uq_guild_channel_policy_channels_guild_channel',
        ),
    )
    op.create_index(
        'ix_guild_channel_policy_channels_guild_id',
        'guild_channel_policy_channels',
        ['guild_id'],
    )
    op.create_index(
        'ix_guild_channel_policy_channels_channel_id',
        'guild_channel_policy_channels',
        ['channel_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_guild_channel_policy_channels_channel_id',
        table_name='guild_channel_policy_channels',
    )
    op.drop_index(
        'ix_guild_channel_policy_channels_guild_id',
        table_name='guild_channel_policy_channels',
    )
    op.drop_table('guild_channel_policy_channels')
    op.drop_table('guild_channel_policies')

    op.drop_index('ix_blocklist_entries_scope_user', table_name='blocklist_entries')
    op.drop_index('ix_blocklist_entries_scope_guild', table_name='blocklist_entries')
    op.drop_index('ix_blocklist_entries_source', table_name='blocklist_entries')
    op.drop_index('ix_blocklist_entries_created_by_id', table_name='blocklist_entries')
    op.drop_index('ix_blocklist_entries_created_at', table_name='blocklist_entries')
    op.drop_index('ix_blocklist_entries_user_id', table_name='blocklist_entries')
    op.drop_index('ix_blocklist_entries_guild_id', table_name='blocklist_entries')
    op.drop_index('ix_blocklist_entries_scope', table_name='blocklist_entries')
    op.drop_table('blocklist_entries')

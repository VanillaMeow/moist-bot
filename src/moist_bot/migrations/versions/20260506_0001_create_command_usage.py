from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '20260506_0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'command_usage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=True),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.Column('author_id', sa.BigInteger(), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('prefix', sa.String(length=100), nullable=False),
        sa.Column('command', sa.String(length=200), nullable=False),
        sa.Column('failed', sa.Boolean(), nullable=False),
        sa.Column('app_command', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_command_usage_app_command', 'command_usage', ['app_command'])
    op.create_index('ix_command_usage_author_id', 'command_usage', ['author_id'])
    op.create_index('ix_command_usage_channel_id', 'command_usage', ['channel_id'])
    op.create_index('ix_command_usage_command', 'command_usage', ['command'])
    op.create_index('ix_command_usage_failed', 'command_usage', ['failed'])
    op.create_index('ix_command_usage_guild_id', 'command_usage', ['guild_id'])
    op.create_index('ix_command_usage_used_at', 'command_usage', ['used_at'])


def downgrade() -> None:
    op.drop_index('ix_command_usage_used_at', table_name='command_usage')
    op.drop_index('ix_command_usage_guild_id', table_name='command_usage')
    op.drop_index('ix_command_usage_failed', table_name='command_usage')
    op.drop_index('ix_command_usage_command', table_name='command_usage')
    op.drop_index('ix_command_usage_channel_id', table_name='command_usage')
    op.drop_index('ix_command_usage_author_id', table_name='command_usage')
    op.drop_index('ix_command_usage_app_command', table_name='command_usage')
    op.drop_table('command_usage')

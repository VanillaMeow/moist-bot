from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260522_0006'
down_revision: str | None = '20260521_0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'guild_honeypot_configs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.Column('log_channel_id', sa.BigInteger(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by_id', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'guild_id',
            name='uq_guild_honeypot_configs_guild_id',
        ),
    )
    op.create_index(
        'ix_guild_honeypot_configs_channel_id',
        'guild_honeypot_configs',
        ['channel_id'],
    )
    op.create_index(
        'ix_guild_honeypot_configs_enabled',
        'guild_honeypot_configs',
        ['enabled'],
    )
    op.create_index(
        'ix_guild_honeypot_configs_guild_id',
        'guild_honeypot_configs',
        ['guild_id'],
    )
    op.create_index(
        'ix_guild_honeypot_configs_log_channel_id',
        'guild_honeypot_configs',
        ['log_channel_id'],
    )

    op.create_table(
        'honeypot_incidents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('config_id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.Column('log_channel_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('message_created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('content_excerpt', sa.String(length=500), nullable=True),
        sa.Column('attachment_count', sa.Integer(), nullable=False),
        sa.Column('trigger_count', sa.Integer(), nullable=False),
        sa.Column('deleted_message_count', sa.Integer(), nullable=False),
        sa.Column('kicked', sa.Boolean(), nullable=False),
        sa.Column('kick_error', sa.String(length=500), nullable=True),
        sa.Column('log_sent', sa.Boolean(), nullable=False),
        sa.Column('log_error', sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ['config_id'],
            ['guild_honeypot_configs.id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_honeypot_incidents_channel_id',
        'honeypot_incidents',
        ['channel_id'],
    )
    op.create_index(
        'ix_honeypot_incidents_config_id',
        'honeypot_incidents',
        ['config_id'],
    )
    op.create_index(
        'ix_honeypot_incidents_guild_id',
        'honeypot_incidents',
        ['guild_id'],
    )
    op.create_index(
        'ix_honeypot_incidents_guild_triggered',
        'honeypot_incidents',
        ['guild_id', 'triggered_at', 'id'],
    )
    op.create_index(
        'ix_honeypot_incidents_guild_user_triggered',
        'honeypot_incidents',
        ['guild_id', 'user_id', 'triggered_at', 'id'],
    )
    op.create_index(
        'ix_honeypot_incidents_kicked',
        'honeypot_incidents',
        ['kicked'],
    )
    op.create_index(
        'ix_honeypot_incidents_log_channel_id',
        'honeypot_incidents',
        ['log_channel_id'],
    )
    op.create_index(
        'ix_honeypot_incidents_log_sent',
        'honeypot_incidents',
        ['log_sent'],
    )
    op.create_index(
        'ix_honeypot_incidents_message_created_at',
        'honeypot_incidents',
        ['message_created_at'],
    )
    op.create_index(
        'ix_honeypot_incidents_message_id',
        'honeypot_incidents',
        ['message_id'],
    )
    op.create_index(
        'ix_honeypot_incidents_triggered_at',
        'honeypot_incidents',
        ['triggered_at'],
    )
    op.create_index(
        'ix_honeypot_incidents_user_id',
        'honeypot_incidents',
        ['user_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_honeypot_incidents_user_id', table_name='honeypot_incidents')
    op.drop_index('ix_honeypot_incidents_triggered_at', table_name='honeypot_incidents')
    op.drop_index('ix_honeypot_incidents_message_id', table_name='honeypot_incidents')
    op.drop_index(
        'ix_honeypot_incidents_message_created_at',
        table_name='honeypot_incidents',
    )
    op.drop_index('ix_honeypot_incidents_log_sent', table_name='honeypot_incidents')
    op.drop_index(
        'ix_honeypot_incidents_log_channel_id',
        table_name='honeypot_incidents',
    )
    op.drop_index('ix_honeypot_incidents_kicked', table_name='honeypot_incidents')
    op.drop_index(
        'ix_honeypot_incidents_guild_user_triggered',
        table_name='honeypot_incidents',
    )
    op.drop_index(
        'ix_honeypot_incidents_guild_triggered',
        table_name='honeypot_incidents',
    )
    op.drop_index('ix_honeypot_incidents_guild_id', table_name='honeypot_incidents')
    op.drop_index('ix_honeypot_incidents_config_id', table_name='honeypot_incidents')
    op.drop_index('ix_honeypot_incidents_channel_id', table_name='honeypot_incidents')
    op.drop_table('honeypot_incidents')

    op.drop_index(
        'ix_guild_honeypot_configs_log_channel_id',
        table_name='guild_honeypot_configs',
    )
    op.drop_index(
        'ix_guild_honeypot_configs_guild_id',
        table_name='guild_honeypot_configs',
    )
    op.drop_index(
        'ix_guild_honeypot_configs_enabled',
        table_name='guild_honeypot_configs',
    )
    op.drop_index(
        'ix_guild_honeypot_configs_channel_id',
        table_name='guild_honeypot_configs',
    )
    op.drop_table('guild_honeypot_configs')

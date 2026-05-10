# ruff: noqa: S608

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260509_0002'
down_revision: str | None = '20260506_0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_scope(
    scope: str,
    select_keys: str,
    group_by: str = '',
    where: str = '',
) -> None:
    where_clause = f'WHERE {where}' if where else ''
    group_by_clause = f'GROUP BY {group_by}' if group_by else ''
    having_clause = '' if group_by else 'HAVING COUNT(*) > 0'

    op.execute(
        f"""
        INSERT INTO command_usage_stats (
            scope,
            guild_id,
            author_id,
            command,
            total_uses,
            failed_uses,
            app_command_uses,
            first_used,
            last_used
        )
        SELECT
            '{scope}',
            {select_keys},
            COUNT(*),
            SUM(CASE WHEN failed THEN 1 ELSE 0 END),
            SUM(CASE WHEN app_command THEN 1 ELSE 0 END),
            MIN(used_at),
            MAX(used_at)
        FROM command_usage
        {where_clause}
        {group_by_clause}
        {having_clause}
        """
    )


def upgrade() -> None:
    op.create_table(
        'command_usage_stats',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=50), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('author_id', sa.BigInteger(), nullable=False),
        sa.Column('command', sa.String(length=200), nullable=False),
        sa.Column('total_uses', sa.Integer(), nullable=False),
        sa.Column('failed_uses', sa.Integer(), nullable=False),
        sa.Column('app_command_uses', sa.Integer(), nullable=False),
        sa.Column('first_used', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'scope',
            'guild_id',
            'author_id',
            'command',
            name='uq_command_usage_stats_scope_key',
        ),
    )
    op.create_index(
        'ix_command_usage_stats_scope_total',
        'command_usage_stats',
        ['scope', 'total_uses'],
    )
    op.create_index(
        'ix_command_usage_stats_scope_guild_total',
        'command_usage_stats',
        ['scope', 'guild_id', 'total_uses'],
    )
    op.create_index(
        'ix_command_usage_stats_scope_author_total',
        'command_usage_stats',
        ['scope', 'author_id', 'total_uses'],
    )
    op.create_index(
        'ix_command_usage_stats_scope_guild_author_total',
        'command_usage_stats',
        ['scope', 'guild_id', 'author_id', 'total_uses'],
    )

    _backfill_scope('global', "0, 0, ''")
    _backfill_scope('global_command', '0, 0, command', group_by='command')
    _backfill_scope(
        'global_guild',
        "COALESCE(guild_id, 0), 0, ''",
        group_by='COALESCE(guild_id, 0)',
    )
    _backfill_scope('global_user', "0, author_id, ''", group_by='author_id')
    _backfill_scope(
        'guild',
        "guild_id, 0, ''",
        group_by='guild_id',
        where='guild_id IS NOT NULL',
    )
    _backfill_scope(
        'guild_command',
        'guild_id, 0, command',
        group_by='guild_id, command',
        where='guild_id IS NOT NULL',
    )
    _backfill_scope(
        'guild_user',
        "guild_id, author_id, ''",
        group_by='guild_id, author_id',
        where='guild_id IS NOT NULL',
    )
    _backfill_scope(
        'guild_user_command',
        'guild_id, author_id, command',
        group_by='guild_id, author_id, command',
        where='guild_id IS NOT NULL',
    )

    op.create_index(
        'ix_command_usage_guild_author_used_at',
        'command_usage',
        ['guild_id', 'author_id', 'used_at'],
    )
    op.create_index(
        'ix_command_usage_guild_command_used_at',
        'command_usage',
        ['guild_id', 'command', 'used_at'],
    )
    op.create_index(
        'ix_command_usage_guild_used_at_author',
        'command_usage',
        ['guild_id', 'used_at', 'author_id'],
    )
    op.create_index(
        'ix_command_usage_used_at_command',
        'command_usage',
        ['used_at', 'command'],
    )


def downgrade() -> None:
    op.drop_index('ix_command_usage_used_at_command', table_name='command_usage')
    op.drop_index('ix_command_usage_guild_used_at_author', table_name='command_usage')
    op.drop_index('ix_command_usage_guild_command_used_at', table_name='command_usage')
    op.drop_index('ix_command_usage_guild_author_used_at', table_name='command_usage')

    op.drop_index(
        'ix_command_usage_stats_scope_guild_author_total',
        table_name='command_usage_stats',
    )
    op.drop_index(
        'ix_command_usage_stats_scope_author_total',
        table_name='command_usage_stats',
    )
    op.drop_index(
        'ix_command_usage_stats_scope_guild_total',
        table_name='command_usage_stats',
    )
    op.drop_index(
        'ix_command_usage_stats_scope_total',
        table_name='command_usage_stats',
    )
    op.drop_table('command_usage_stats')

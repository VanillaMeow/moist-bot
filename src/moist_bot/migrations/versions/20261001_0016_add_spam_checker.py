"""Add spam configuration and incident tables, reusing existing tracker storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20261001_0016'
down_revision: str | None = '20260915_0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create guild settings, entity exemptions, and indexed incident history."""

    op.create_table(
        'guild_spam_configs',
        sa.Column('guild_id', sa.BigInteger(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('settings_json', sa.String(), nullable=False),
        sa.Column('updated_by_id', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        'guild_spam_exemptions',
        sa.Column('guild_id', sa.BigInteger(), primary_key=True),
        sa.Column('kind', sa.String(20), primary_key=True),
        sa.Column('entity_id', sa.BigInteger(), primary_key=True),
    )

    op.create_table(
        'spam_incidents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('rule', sa.String(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('reason', sa.String(), nullable=False),
        sa.Column('content_excerpt', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('messages_deleted', sa.Integer(), nullable=False),
        sa.Column('cleanup_error', sa.String(), nullable=True),
        sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=False),
    )

    # Incident history is queried by guild in newest-first order
    op.create_index(
        'ix_spam_incidents_guild_triggered',
        'spam_incidents',
        ['guild_id', 'triggered_at'],
    )


def downgrade() -> None:
    """Remove spam-owned data while preserving other services' tracker rows."""

    # Shared tracker storage remains, so remove only the spam namespace
    op.execute(sa.text("DELETE FROM tracker_states WHERE namespace = 'spam'"))

    op.drop_index('ix_spam_incidents_guild_triggered', table_name='spam_incidents')
    op.drop_table('spam_incidents')
    op.drop_table('guild_spam_exemptions')
    op.drop_table('guild_spam_configs')

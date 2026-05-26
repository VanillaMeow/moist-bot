from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260526_0009'
down_revision: str | None = '20260524_0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'guild_honeypot_configs',
        sa.Column('alert_message_id', sa.BigInteger(), nullable=True),
    )
    op.create_index(
        'ix_guild_honeypot_configs_alert_message_id',
        'guild_honeypot_configs',
        ['alert_message_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_guild_honeypot_configs_alert_message_id',
        table_name='guild_honeypot_configs',
    )
    op.drop_column('guild_honeypot_configs', 'alert_message_id')

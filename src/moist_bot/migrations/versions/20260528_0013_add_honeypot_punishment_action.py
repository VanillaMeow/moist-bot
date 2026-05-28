from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260528_0013'
down_revision: str | None = '20260527_0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index('ix_honeypot_incidents_softbanned', table_name='honeypot_incidents')

    with op.batch_alter_table('honeypot_incidents') as batch_op:
        batch_op.alter_column('softbanned', new_column_name='punishment_succeeded')
        batch_op.alter_column('softban_error', new_column_name='punishment_error')
        batch_op.add_column(
            sa.Column(
                'punishment_action',
                sa.String(length=20),
                nullable=False,
                server_default='softban',
            )
        )

    op.create_index(
        'ix_honeypot_incidents_punishment_action',
        'honeypot_incidents',
        ['punishment_action'],
    )
    op.create_index(
        'ix_honeypot_incidents_punishment_succeeded',
        'honeypot_incidents',
        ['punishment_succeeded'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_honeypot_incidents_punishment_succeeded',
        table_name='honeypot_incidents',
    )
    op.drop_index(
        'ix_honeypot_incidents_punishment_action',
        table_name='honeypot_incidents',
    )

    with op.batch_alter_table('honeypot_incidents') as batch_op:
        batch_op.drop_column('punishment_action')
        batch_op.alter_column('punishment_error', new_column_name='softban_error')
        batch_op.alter_column('punishment_succeeded', new_column_name='softbanned')

    op.create_index(
        'ix_honeypot_incidents_softbanned',
        'honeypot_incidents',
        ['softbanned'],
    )

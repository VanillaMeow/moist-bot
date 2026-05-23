from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260523_0007'
down_revision: str | None = '20260522_0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index('ix_honeypot_incidents_kicked', table_name='honeypot_incidents')

    with op.batch_alter_table('honeypot_incidents') as batch_op:
        batch_op.alter_column(
            'deleted_message_count',
            new_column_name='delete_message_seconds',
        )
        batch_op.alter_column('kicked', new_column_name='softbanned')
        batch_op.alter_column('kick_error', new_column_name='softban_error')

    op.create_index(
        'ix_honeypot_incidents_softbanned',
        'honeypot_incidents',
        ['softbanned'],
    )


def downgrade() -> None:
    op.drop_index('ix_honeypot_incidents_softbanned', table_name='honeypot_incidents')

    with op.batch_alter_table('honeypot_incidents') as batch_op:
        batch_op.alter_column('softban_error', new_column_name='kick_error')
        batch_op.alter_column('softbanned', new_column_name='kicked')
        batch_op.alter_column(
            'delete_message_seconds',
            new_column_name='deleted_message_count',
        )

    op.create_index(
        'ix_honeypot_incidents_kicked',
        'honeypot_incidents',
        ['kicked'],
    )

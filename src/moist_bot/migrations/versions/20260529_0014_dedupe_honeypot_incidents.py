from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260529_0014'
down_revision: str | None = '20260528_0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM honeypot_incidents
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM honeypot_incidents
            GROUP BY guild_id, message_id
        )
        """
    )
    op.execute('DELETE FROM honeypot_guild_stats')
    op.execute(
        """
        INSERT INTO honeypot_guild_stats (guild_id, total_incidents)
        SELECT guild_id, COUNT(id)
        FROM honeypot_incidents
        GROUP BY guild_id
        """
    )
    op.execute('DELETE FROM honeypot_user_stats')
    op.execute(
        """
        INSERT INTO honeypot_user_stats (guild_id, user_id, total_incidents)
        SELECT guild_id, user_id, COUNT(id)
        FROM honeypot_incidents
        GROUP BY guild_id, user_id
        """
    )
    op.create_index(
        'uq_honeypot_incidents_guild_message',
        'honeypot_incidents',
        ['guild_id', 'message_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        'uq_honeypot_incidents_guild_message',
        table_name='honeypot_incidents',
    )

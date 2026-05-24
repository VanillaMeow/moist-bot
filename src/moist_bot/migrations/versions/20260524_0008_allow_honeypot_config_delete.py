from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260524_0008'
down_revision: str | None = '20260523_0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK_NAMING_CONVENTION = {
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
}
CONFIG_FK_NAME = 'fk_honeypot_incidents_config_id_guild_honeypot_configs'


def upgrade() -> None:
    with op.batch_alter_table(
        'honeypot_incidents',
        naming_convention=FK_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(CONFIG_FK_NAME, type_='foreignkey')
        batch_op.alter_column(
            'config_id',
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.create_foreign_key(
            CONFIG_FK_NAME,
            'guild_honeypot_configs',
            ['config_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    op.execute('DELETE FROM honeypot_incidents WHERE config_id IS NULL')

    with op.batch_alter_table(
        'honeypot_incidents',
        naming_convention=FK_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(CONFIG_FK_NAME, type_='foreignkey')
        batch_op.alter_column(
            'config_id',
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            CONFIG_FK_NAME,
            'guild_honeypot_configs',
            ['config_id'],
            ['id'],
            ondelete='RESTRICT',
        )

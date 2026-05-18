from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = '20260518_0004'
down_revision: str | None = '20260516_0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ENTRY_TABLE = 'black' + 'list_entries'
NEW_ENTRY_TABLE = 'blocklist_entries'

OLD_INDEXES = (
    'ix_black' + 'list_entries_scope',
    'ix_black' + 'list_entries_guild_id',
    'ix_black' + 'list_entries_user_id',
    'ix_black' + 'list_entries_created_at',
    'ix_black' + 'list_entries_created_by_id',
    'ix_black' + 'list_entries_source',
    'ix_black' + 'list_entries_scope_guild',
    'ix_black' + 'list_entries_scope_user',
)

NEW_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ('ix_blocklist_entries_scope', ['scope']),
    ('ix_blocklist_entries_guild_id', ['guild_id']),
    ('ix_blocklist_entries_user_id', ['user_id']),
    ('ix_blocklist_entries_created_at', ['created_at']),
    ('ix_blocklist_entries_created_by_id', ['created_by_id']),
    ('ix_blocklist_entries_source', ['source']),
    ('ix_blocklist_entries_scope_guild', ['scope', 'guild_id']),
    ('ix_blocklist_entries_scope_user', ['scope', 'user_id']),
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if OLD_ENTRY_TABLE not in tables or NEW_ENTRY_TABLE in tables:
        return

    for index_name in OLD_INDEXES:
        op.drop_index(index_name, table_name=OLD_ENTRY_TABLE)

    op.rename_table(OLD_ENTRY_TABLE, NEW_ENTRY_TABLE)

    for index_name, columns in NEW_INDEXES:
        op.create_index(index_name, NEW_ENTRY_TABLE, columns)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if NEW_ENTRY_TABLE not in tables or OLD_ENTRY_TABLE in tables:
        return

    for index_name, _columns in NEW_INDEXES:
        op.drop_index(index_name, table_name=NEW_ENTRY_TABLE)

    op.rename_table(NEW_ENTRY_TABLE, OLD_ENTRY_TABLE)

    for index_name, (_new_index_name, columns) in zip(
        OLD_INDEXES, NEW_INDEXES, strict=True
    ):
        op.create_index(index_name, OLD_ENTRY_TABLE, columns)

from __future__ import annotations

from datetime import UTC, datetime

from anyio import Path

# Meta
ROOT_PACKAGE = __package__.split('.', maxsplit=1)[0] if __package__ else None
ROOT_PATH = Path(__file__).parent
PROJECT_ROOT_PATH = ROOT_PATH.parents[1]
ROOT_PACKAGE_PROJECT_PATH = ROOT_PATH.relative_to(PROJECT_ROOT_PATH)
COGS_FOLDER_PATH = ROOT_PATH / 'cogs'
COGS_PROJECT_PATH = COGS_FOLDER_PATH.relative_to(PROJECT_ROOT_PATH)
ASSETS_FOLDER_PATH = ROOT_PATH / 'assets'
MIGRATIONS_FOLDER_PATH = ROOT_PATH / 'migrations'
MIGRATIONS_PROJECT_PATH = MIGRATIONS_FOLDER_PATH.relative_to(PROJECT_ROOT_PATH)
DEPENDENCY_FILES = frozenset({Path('pyproject.toml'), Path('uv.lock')})

# Database
DB_NAME = 'moist.db'
DB_FOLDER_PATH = ROOT_PATH / 'db'
DB_PATH = DB_FOLDER_PATH / DB_NAME

# Logging
LOGS_FOLDER_PATH = ROOT_PATH / 'logs'

DATETIME_NEVER = datetime.fromtimestamp(0, tz=UTC)

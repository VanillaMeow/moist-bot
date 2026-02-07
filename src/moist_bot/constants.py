from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

# Meta
ROOT_PACKAGE = __package__.split('.', maxsplit=1)[0] if __package__ else None
ROOT_PATH = Path(__file__).parent.resolve()
COGS_FOLDER_PATH = ROOT_PATH / 'cogs'
ASSETS_FOLDER_PATH = ROOT_PATH / 'assets'

# Database
DB_NAME = 'moist.db'
DB_FOLDER_PATH = ROOT_PATH / 'db'
DB_PATH = DB_FOLDER_PATH / DB_NAME

# Logging
LOGS_FOLDER_PATH = ROOT_PATH / 'logs'

DATETIME_NEVER = datetime.fromtimestamp(0, tz=UTC)

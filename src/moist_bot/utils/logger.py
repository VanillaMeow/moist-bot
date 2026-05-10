from __future__ import annotations

__all__ = ('setup_alembic_logging', 'setup_logging')

import logging
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, cast

from colorama import Back, Fore, Style

from moist_bot.constants import LOGS_FOLDER_PATH

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import Any, ClassVar


class _ColorFormatter(logging.Formatter):
    LEVEL_COLORS: ClassVar[list[tuple[int, str]]] = [
        (logging.DEBUG, Fore.LIGHTBLACK_EX),
        (logging.INFO, Fore.LIGHTBLUE_EX),
        (logging.WARNING, Fore.YELLOW),
        (logging.ERROR, Fore.RED),
        (logging.CRITICAL, Back.RED),
    ]

    FORMATS: ClassVar[dict[int, logging.Formatter]] = {
        level: logging.Formatter(
            f'{Fore.LIGHTBLACK_EX}%(asctime)s,%(msecs)03d{Style.RESET_ALL} '
            f'{color}%(levelname)-0s{Style.RESET_ALL} '
            f'{Fore.MAGENTA}%(name)s{Style.RESET_ALL} '
            '%(message)s',
            '%H:%M:%S',
        )
        for level, color in LEVEL_COLORS
    }

    def format(self, record: logging.LogRecord) -> str:
        formatter = self.FORMATS.get(record.levelno)
        if formatter is None:
            formatter = self.FORMATS[logging.DEBUG]

        # Override the traceback to always print in red
        if record.exc_info:
            text = formatter.formatException(record.exc_info)
            record.exc_text = f'{Fore.RED}{text}{Style.RESET_ALL}'

        output = formatter.format(record)

        # Remove the cache layer
        record.exc_text = None
        return output


def setup_alembic_logging() -> None:
    """Configure Alembic console logging with the bot formatter."""

    root_log = logging.getLogger()
    stream_handler: logging.StreamHandler[Any] | None = None

    for handler in root_log.handlers:
        if isinstance(handler, logging.StreamHandler):
            stream_handler = cast('logging.StreamHandler[Any]', handler)
            break

    if stream_handler is None:
        stream_handler = logging.StreamHandler()
        root_log.addHandler(stream_handler)

    stream_handler.setFormatter(_ColorFormatter())
    stream_handler.setLevel(logging.INFO)

    root_log.setLevel(logging.WARNING)
    logging.getLogger('alembic').setLevel(logging.INFO)
    logging.getLogger('sqlalchemy').setLevel(logging.WARNING)


@contextmanager
def setup_logging() -> Generator[None, Any]:
    root_log = logging.getLogger()

    try:
        # __enter__
        handler = logging.StreamHandler()
        handler.setFormatter(_ColorFormatter())
        root_log.addHandler(handler)
        root_log.setLevel(logging.DEBUG)

        logging.getLogger('discord').setLevel(logging.INFO)
        logging.getLogger('discord.http').setLevel(logging.WARNING)
        logging.getLogger('discord.gateway').setLevel(logging.DEBUG)
        # logging.getLogger('discord.state').addFilter(RemoveNoise())

        # Set stream handlers to INFO level
        for handler in root_log.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setLevel(logging.INFO)

        # Setup file logging
        max_bytes = 32 * 1024 * 1024  # 32 MiB
        dt_fmt = '%Y-%m-%d %H:%M:%S'
        fmt = '[{asctime}] [{levelname:<8}] {name}: {message}'

        file_handler = RotatingFileHandler(
            filename=LOGS_FOLDER_PATH / 'discord.log',
            maxBytes=max_bytes,
            encoding='utf-8',
            backupCount=3,
            mode='w',
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(fmt, dt_fmt, style='{'))
        root_log.addHandler(file_handler)

        yield
    finally:
        # __exit__
        handlers = root_log.handlers[:]
        for handler in handlers:
            handler.close()
            root_log.removeHandler(handler)

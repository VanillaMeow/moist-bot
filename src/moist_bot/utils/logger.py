from __future__ import annotations

__all__ = ('setup_alembic_logging', 'setup_logging')

import logging
from contextlib import contextmanager
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from queue import SimpleQueue
from typing import TYPE_CHECKING, cast

from colorama import Back, Fore, Style

from moist_bot.constants import LOGS_FOLDER_PATH

_FILE_RETENTION_DAYS = 30
_FILE_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
_FILE_FORMAT = '[{asctime}] [{levelname:<8}] {name}: {message}'

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


class _LocalQueueHandler(QueueHandler):
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record


class _LoggerNameFilter(logging.Filter):
    def __init__(self, logger_name: str, *, include: bool) -> None:
        super().__init__()
        self._logger_name = logger_name
        self._logger_prefix = f'{logger_name}.'
        self._include = include

    def filter(self, record: logging.LogRecord) -> bool:
        matches_logger = record.name == self._logger_name or record.name.startswith(
            self._logger_prefix
        )
        return matches_logger if self._include else not matches_logger


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


def _create_debug_file_handler(
    filename: str,
    *,
    logger_name: str | None = None,
    include_logger: bool = True,
) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        filename=LOGS_FOLDER_PATH / filename,
        when='midnight',
        backupCount=_FILE_RETENTION_DAYS,
        encoding='utf-8',
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_FILE_FORMAT, _FILE_DATE_FORMAT, style='{'))

    if logger_name is not None:
        handler.addFilter(_LoggerNameFilter(logger_name, include=include_logger))

    return handler


@contextmanager
def setup_logging() -> Generator[None, Any]:
    root_log = logging.getLogger()

    # Create queue handler
    log_queue: SimpleQueue[logging.LogRecord | None] = SimpleQueue()
    queue_handler = _LocalQueueHandler(log_queue)
    queue_handler.setLevel(logging.DEBUG)

    # Create stream handler
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(_ColorFormatter())
    stream_handler.setLevel(logging.INFO)

    # Create file handlers
    file_handler = _create_debug_file_handler(
        'discord.log',
        logger_name='aiosqlite',
        include_logger=False,
    )
    aiosqlite_file_handler = _create_debug_file_handler(
        'aiosqlite.log',
        logger_name='aiosqlite',
    )

    # Create listener
    listener = QueueListener(
        log_queue,
        stream_handler,
        file_handler,
        aiosqlite_file_handler,
        respect_handler_level=True,
    )
    listener_started = False

    # ruff: noqa: PLW0717
    try:
        # __enter__
        # Clear root handlers
        for handler in root_log.handlers[:]:
            root_log.removeHandler(handler)
            handler.close()

        # Install queue handler
        root_log.setLevel(logging.DEBUG)
        root_log.addHandler(queue_handler)

        # Set logging levels
        logging.getLogger('discord').setLevel(logging.INFO)
        logging.getLogger('discord.http').setLevel(logging.WARNING)
        logging.getLogger('discord.gateway').setLevel(logging.DEBUG)
        logging.getLogger('aiosqlite').setLevel(logging.DEBUG)
        # logging.getLogger('discord.state').addFilter(RemoveNoise())

        # Start listener
        listener.start()
        listener_started = True

        yield
    finally:
        # __exit__
        if listener_started:
            listener.stop()

        if queue_handler in root_log.handlers[:]:
            root_log.removeHandler(queue_handler)

        for handler in (
            queue_handler,
            stream_handler,
            file_handler,
            aiosqlite_file_handler,
        ):
            handler.close()

from __future__ import annotations

import logging
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

import discord

from moist_bot.constants import LOGS_FOLDER_PATH


@contextmanager
def setup_logging():
    log = logging.getLogger()

    try:
        discord.utils.setup_logging(level=logging.DEBUG)
        # __enter__
        logging.getLogger('discord').setLevel(logging.INFO)
        logging.getLogger('discord.http').setLevel(logging.WARNING)
        logging.getLogger('discord.gateway').setLevel(logging.DEBUG)
        # logging.getLogger('discord.state').addFilter(RemoveNoise())

        # Set stream handlers to INFO level
        for handler in log.handlers:
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
        log.addHandler(file_handler)

        yield
    finally:
        # __exit__
        handlers = log.handlers[:]
        for handler in handlers:
            handler.close()
            log.removeHandler(handler)

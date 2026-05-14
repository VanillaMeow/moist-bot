from __future__ import annotations

__all__ = ('settings',)

from functools import cached_property

from discord import Object
from pydantic_settings import BaseSettings, SettingsConfigDict

from moist_bot.constants import DB_PATH


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

    # Bot
    token: str = ''
    test_guild_id: int = 294545830742982656
    logs_channel_id: int = 1504392687086866473

    # Database
    database_url: str = f'sqlite+aiosqlite:///{DB_PATH}'

    @cached_property
    def test_guild(self) -> Object:
        return Object(self.test_guild_id)

    @cached_property
    def logs_channel(self) -> Object:
        return Object(self.logs_channel_id)


settings = Settings()

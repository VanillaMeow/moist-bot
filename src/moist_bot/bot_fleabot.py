from __future__ import annotations

from typing import TYPE_CHECKING, Unpack

from discord.ext import commands

from moist_bot.bot import MoistBot
from moist_bot.settings import settings

if TYPE_CHECKING:
    from typing import Final

    from discord import Message

    from moist_bot.bot import BotOptions


FLEABOT_EXTENSIONS: Final = (
    'errorhandle',
    'owner_debug',
    'blocklist',
    'honeypot',
    'owner',
    'stats',
    'purge',
    'meta',
    'jsk',
)
BOT_PREFIXES = ('fb ', 'Fb ')


def _get_prefix(bot: commands.Bot, message: Message) -> list[str]:
    return commands.when_mentioned_or(*BOT_PREFIXES)(bot, message)


class FleaBot(MoistBot):
    def __init__(self, **kwargs: Unpack[BotOptions]) -> None:
        kwargs.setdefault('command_prefix', _get_prefix)
        super().__init__(startup_extensions=FLEABOT_EXTENSIONS, **kwargs)

    async def start(
        self, token: str = settings.fleabot_token, *, reconnect: bool = True
    ) -> None:
        await super().start(token=token, reconnect=reconnect)

from __future__ import annotations

from typing import TYPE_CHECKING, Unpack

from moist_bot.bot import MoistBot
from moist_bot.settings import settings

if TYPE_CHECKING:
    from typing import Final

    from moist_bot.bot import BotOptions


FLEABOT_EXTENSION_NAMES: Final = (
    'blocklist',
    'errorhandle',
    'honeypot',
    'meta',
    'owner',
    'stats',
)


class FleaBot(MoistBot):
    def __init__(self, **kwargs: Unpack[BotOptions]) -> None:
        super().__init__(extension_names=FLEABOT_EXTENSION_NAMES, **kwargs)

    async def start(
        self, token: str = settings.fleabot_token, *, reconnect: bool = True
    ) -> None:
        await super().start(token=token, reconnect=reconnect)

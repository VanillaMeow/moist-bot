from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from jishaku.cog import OPTIONAL_FEATURES, STANDARD_FEATURES

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot


class JishakuDebugCog(*OPTIONAL_FEATURES, *STANDARD_FEATURES):  # type: ignore[reportUntypedBaseClass]
    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{GEAR}')


async def setup(client: MoistBot):
    await client.add_cog(JishakuDebugCog(bot=client))

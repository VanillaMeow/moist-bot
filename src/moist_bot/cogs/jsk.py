from __future__ import annotations

from typing import TYPE_CHECKING

from jishaku.cog import OPTIONAL_FEATURES, STANDARD_FEATURES

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot


class JishakuDebugCog(*OPTIONAL_FEATURES, *STANDARD_FEATURES):  # type: ignore[reportUntypedBaseClass]
    pass


async def setup(bot: MoistBot):
    await bot.add_cog(JishakuDebugCog(bot=bot))

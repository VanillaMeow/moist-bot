from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    import discord
    from discord import Message, app_commands
    from discord.ext import commands
    from discord.ext.commands.bot import _BotOptions  # type: ignore[]

    class BotOptions(_BotOptions, total=False):
        command_prefix: Callable[[commands.Bot, Message], list[str]]
        help_attrs: dict[str, Any]
        case_insensitive: bool
        intents: discord.Intents
        tree_cls: type[app_commands.CommandTree[Any]]


@dataclass(frozen=True, slots=True)
class SoftbanResult:
    """Outcome of a softban attempt."""

    softbanned: bool
    error: str | None
    ban_applied: bool

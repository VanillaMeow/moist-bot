from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from moist_bot.utils.context import Context


def can_handle(ctx: Context, permission: str) -> bool:
    """Checks if bot has permissions or is in DMs right now."""
    return isinstance(ctx.channel, discord.DMChannel) or (
        ctx.guild is not None
        and getattr(ctx.channel.permissions_for(ctx.guild.me), permission)
    )


class HelpFormat(commands.DefaultHelpCommand):
    context: Context  # pyright: ignore[reportIncompatibleVariableOverride]

    def get_destination(self, no_pm: bool = False):
        if no_pm:
            return self.context.channel
        return self.context.author

    async def send_error_message(self, error: str, /) -> None:
        """Sends an error message to the destination."""
        destination = self.get_destination(no_pm=True)
        await destination.send(error)

    async def send_command_help(
        self, command: commands.Command[Any, ..., Any], /
    ) -> None:
        """Sends the help for a single command."""
        self.add_command_formatting(command)
        self.paginator.close_page()
        await self.send_pages(no_pm=True)

    async def send_pages(self, no_pm: bool = False) -> None:
        """Sends the help pages to the destination."""
        try:
            if can_handle(self.context, 'add_reactions'):
                await self.context.message.add_reaction(chr(0x2709))
        except discord.Forbidden:
            pass

        try:
            destination = self.get_destination(no_pm=no_pm)
            for page in self.paginator.pages:
                await destination.send(page)
        except discord.Forbidden:
            destination = self.get_destination(no_pm=True)
            await destination.send("Couldn't send help to you due to blocked DMs...")

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from typing import Any

    from moist_bot.utils.context import Context


def has_permissions_or_dm(**perms: bool) -> Any:
    """Check that passes in DMs or when the user has channel permissions."""

    invalid = set(perms) - set(discord.Permissions.VALID_FLAGS)
    if invalid:
        msg = f'Invalid permission(s): {", ".join(invalid)}'
        raise TypeError(msg)

    def predicate(ctx: Context) -> bool:
        if ctx.guild is None:
            return True

        permissions = ctx.permissions
        missing = [
            perm for perm, value in perms.items() if getattr(permissions, perm) != value
        ]
        if not missing:
            return True

        raise commands.MissingPermissions(missing)

    return commands.check(predicate)


def has_guild_permissions_or_dm(**perms: bool) -> Any:
    """Check that passes in DMs or when the user has guild permissions."""

    invalid = set(perms) - set(discord.Permissions.VALID_FLAGS)
    if invalid:
        msg = f'Invalid permission(s): {", ".join(invalid)}'
        raise TypeError(msg)

    def predicate(ctx: Context) -> bool:
        if ctx.guild is None:
            return True

        if not isinstance(ctx.author, discord.Member):
            raise commands.NoPrivateMessage

        permissions = ctx.author.guild_permissions
        missing = [
            perm for perm, value in perms.items() if getattr(permissions, perm) != value
        ]
        if not missing:
            return True

        raise commands.MissingPermissions(missing)

    return commands.check(predicate)

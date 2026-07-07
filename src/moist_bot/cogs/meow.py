# ruff: noqa: S311

from __future__ import annotations

from random import choice, randint
from typing import TYPE_CHECKING, ClassVar, cast

import discord
import pyperclip
from discord.ext import commands

from moist_bot.settings import settings

pyperclip.determine_clipboard()

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


WARN_TOO_LONG = ":warning: I can't meow that long >~<"
WARN_TOO_SHORT = ':warning: Amount must be at least 1.'
WARN_LOW_PERMS = ':warning: You can only meow up to 10 words in this channel.'

MOD_PERMS_MAX_SIZE = 500
LOW_PERMS_MAX_SIZE = 10


class Meow(commands.Cog):
    """Generate a random meow."""

    word_list: ClassVar[list[str]] = [
        'nya~',
        'meow',
        'mrow',
        'nyah~',
        'mew',
        'mrooowww',
        'meoow',
        'mrrrp',
        'mrp',
        'meoww',
        'nyaaaaa~',
        ':3',
        'uwu',
        'owo',
        'owu',
        'UwU',
        'OwO',
        'tehe',
        'rawr',
        'purr',
    ]

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{CAT FACE}')

    @commands.command()
    @commands.cooldown(rate=1, per=60, type=commands.BucketType.member)
    async def meow(self, ctx: Context, random_size: int | None = None):
        """Generate a random meow."""

        mod_perms: bool = True
        is_guild: bool = ctx.guild is not None

        if is_guild:
            author = cast('discord.Member', ctx.author)
            mod_perms = ctx.channel.permissions_for(author).manage_messages

        size_limit: int = MOD_PERMS_MAX_SIZE if mod_perms else LOW_PERMS_MAX_SIZE

        # Limit size if `random_size` is specified
        if random_size is not None:
            if random_size < 1:
                return await ctx.reply(WARN_TOO_SHORT)
            if random_size > size_limit:
                return await ctx.reply(WARN_TOO_LONG if mod_perms else WARN_LOW_PERMS)
        else:
            rng_limit = 30 if mod_perms else size_limit
            random_size = randint(5, rng_limit)

        random_words = [choice(self.word_list) for _ in range(random_size)]
        random_sentence = ' '.join(random_words)

        if len(random_sentence) > 2000:
            return await ctx.reply(WARN_TOO_LONG)

        # Automatically copy the contents to the clipboard for bot owners :3
        if not settings.use_fleabot and await self.bot.is_owner(ctx.author):
            pyperclip.copy(random_sentence)

        await ctx.reply(random_sentence)


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(Meow(bot))

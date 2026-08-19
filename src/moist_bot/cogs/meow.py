# ruff: noqa: S311

from __future__ import annotations

from random import choices, randint
from typing import TYPE_CHECKING, ClassVar, NamedTuple, cast

import discord
import pyperclip
from discord.ext import commands

from moist_bot.settings import settings

pyperclip.determine_clipboard()

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


MOD_PERMS_MAX_SIZE = 350
LOW_PERMS_MAX_SIZE = 10

WARN_TOO_LONG = ":warning: I can't meow that long >~<"
WARN_TOO_SHORT = ':warning: Amount must be at least 1.'
WARN_LOW_PERMS = f':warning: You can only meow up to {LOW_PERMS_MAX_SIZE} words in this channel.'  # fmt: skip


def maybe(percentage: int) -> bool:
    """Return whether a percentage chance succeeds."""
    return randint(1, 100) <= percentage


class WeightedItem[T](NamedTuple):
    item: T
    weight: int


class WeightedCollection[T]:
    """Weighted items with cached choice data."""

    __slots__ = ('items', 'weights')

    items: tuple[T, ...]
    weights: tuple[int, ...]

    def __init__(self, *options: WeightedItem[T]):
        # Cache item and weight tuples for repeated random choices
        self.items = tuple(option.item for option in options)
        self.weights = tuple(option.weight for option in options)

    def choice(self) -> T:
        """Pick one weighted item option."""
        return choices(self.items, weights=self.weights, k=1)[0]


class MeowGenerator:
    __slots__ = ()

    EMOTE_CHANCE: int = 15
    TRAILING_BIT_CHANCE: int = 35
    STRETCH_CHANCE: int = 35

    BASE_WORDS = WeightedCollection(
        WeightedItem('meow', weight=10),
        WeightedItem('nya', weight=9),
        WeightedItem('nyah', weight=9),
        WeightedItem('mrow', weight=5),
        WeightedItem('tehe', weight=5),
        WeightedItem('mrrp', weight=5),
        WeightedItem('purr', weight=3),
        WeightedItem('mew', weight=2),
        WeightedItem('mrp', weight=2),
        WeightedItem('rawr', weight=1),
    )
    EMOTES = WeightedCollection(
        WeightedItem(':3', weight=10),
        WeightedItem('uwu', weight=4),
        WeightedItem('owo', weight=3),
        WeightedItem('owu', weight=3),
        WeightedItem('UwU', weight=3),
        WeightedItem('OwO', weight=3),
    )
    TRAILING_BITS = WeightedCollection(
        WeightedItem('~', weight=10),
        WeightedItem('!', weight=5),
        WeightedItem('!!', weight=3),
        WeightedItem('...', weight=3),
    )

    stretchable_chars: ClassVar[set[str]] = {'a', 'e', 'o', 'r', 'u', 'w', 'y'}

    @classmethod
    def sentence(cls, size: int) -> str:
        """Generate a sentence with the requested number of meow words."""
        return ' '.join(cls.word() for _ in range(size))

    @classmethod
    def word(cls) -> str:
        """Generate one meow word by mutating a known-good base shape."""
        if maybe(cls.EMOTE_CHANCE):
            return cls.EMOTES.choice()

        word = cls.BASE_WORDS.choice()
        return cls._mutate_word(word)

    @classmethod
    def _mutate_word(cls, word: str) -> str:
        """Randomly stretch and decorate an existing meow word."""
        word = cls._stretch_word(word)
        trailing_bit = cls.TRAILING_BITS.choice()
        if not word.endswith(trailing_bit) and maybe(cls.TRAILING_BIT_CHANCE):
            word += trailing_bit

        return word

    @classmethod
    def _stretch_word(cls, word: str) -> str:
        """Randomly duplicate stretchable characters in a word."""
        return ''.join(
            char * randint(1, 3)
            if char.lower() in cls.stretchable_chars and maybe(cls.STRETCH_CHANCE)
            else char
            for char in word
        )


class Meow(commands.Cog):
    """Generate a random meow."""

    def __init__(self, bot: MoistBot):
        self.bot: MoistBot = bot

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\N{CAT FACE}')

    @commands.command()
    @commands.cooldown(rate=1, per=300, type=commands.BucketType.member)
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

        random_sentence = MeowGenerator.sentence(random_size)

        if len(random_sentence) > 2000:
            return await ctx.reply(WARN_TOO_LONG)

        # Automatically copy the contents to the clipboard for bot owners :3
        if not settings.use_fleabot and await self.bot.is_owner(ctx.author):
            pyperclip.copy(random_sentence)

        await ctx.reply(random_sentence)


async def setup(bot: MoistBot) -> None:
    await bot.add_cog(Meow(bot))

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
    from moist_bot.utils.context import Context


class AvatarEmbed(discord.Embed):
    def __init__(
        self,
        avatar_url: str,
        user: discord.User | discord.Member,
        *,
        source: str = 'base',
    ):
        super().__init__(type='image', color=discord.Color.random())
        self.set_image(url=avatar_url)
        self.set_author(
            name=f"{user.display_name}'s {source} avatar",
            url=avatar_url,
            icon_url=avatar_url,
        )


class Avatar(commands.Cog):
    def __init__(self, client: MoistBot):
        self.client: MoistBot = client

    @property
    def display_emoji(self) -> discord.PartialEmoji:
        return discord.PartialEmoji(name='\U0001f465')

    @commands.cooldown(rate=1, per=2, type=commands.BucketType.user)
    @commands.command()
    async def avatar(self, ctx: Context, *, user: discord.Member = commands.Author):
        """Display a user's avatar."""

        embeds: list[discord.Embed] = []

        # First embed with base avatar
        avatar_url = user.avatar.url if user.avatar else user.default_avatar.url
        embed = AvatarEmbed(avatar_url, user)
        embeds.append(embed)

        # Second embed with guild avatar (if exists)
        if hasattr(user, 'guild_avatar') and user.guild_avatar:
            avatar_url = user.guild_avatar.url
            embed = AvatarEmbed(avatar_url, user, source='guild')
            embeds.append(embed)

        # Send embeds
        await ctx.reply(embeds=embeds)


async def setup(client: MoistBot) -> None:
    await client.add_cog(Avatar(client))

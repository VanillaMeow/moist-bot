# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from moist_bot.bot import MoistBot
else:
    type MoistBot = commands.Bot


class ConfirmationView(discord.ui.View):
    def __init__(self, *, timeout: float, author_id: int, delete_after: bool) -> None:
        super().__init__(timeout=timeout)
        self.value: bool | None = None
        self.delete_after: bool = delete_after
        self.author_id: int = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            'This confirmation dialog is not for you.', ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        if self.delete_after and self.message:
            await self.message.delete()

    @discord.ui.button(label='Confirm', style=discord.ButtonStyle.green)
    async def confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ):
        self.value = True
        await interaction.response.defer()
        if self.delete_after:
            await interaction.delete_original_response()

        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.red)
    async def cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ):
        self.value = False
        await interaction.response.defer()
        if self.delete_after:
            await interaction.delete_original_response()

        self.stop()


class Context(commands.Context[MoistBot]):
    prefix: str
    command: commands.Command[Any, ..., Any]
    bot: MoistBot

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        # we need this for our cache key strategy
        return '<Context>'

    @discord.utils.cached_property
    def replied_reference(self) -> discord.MessageReference | None:
        ref = self.message.reference
        if ref and isinstance(ref.resolved, discord.Message):
            return ref.resolved.to_reference()
        return None

    @discord.utils.cached_property
    def replied_message(self) -> discord.Message | None:
        ref = self.message.reference
        if ref and isinstance(ref.resolved, discord.Message):
            return ref.resolved
        return None

    @staticmethod
    def tick(opt: bool | None, label: str | None = None) -> str:
        lookup = {
            True: '<:greenTick:330090705336664065>',
            False: '<:redTick:330090723011592193>',
            None: '<:greyTick:563231201280917524>',
        }
        emoji = lookup.get(opt, '<:redTick:330090723011592193>')
        if label is not None:
            return f'{emoji}: {label}'
        return emoji

    async def prompt(
        self,
        message: str,
        *,
        timeout: float = 60.0,
        delete_after: bool = True,
        author_id: int | None = None,
    ) -> bool | None:
        """An interactive reaction confirmation dialog.

        github.com/Rapptz/RoboDanny/blob/rewrite/cogs/utils/context.py#L198C22-L198C22

        Parameters
        -----------
        message: str
            The message to show along with the prompt.
        timeout: float
            How long to wait before returning.
        delete_after: bool
            Whether to delete the confirmation message after we're done.
        author_id: Optional[int]
            The member who should respond to the prompt. Defaults to the author of the
            Context's message.

        Returns
        --------
        Optional[bool]
            ``True`` if explicit confirm,
            ``False`` if explicit deny,
            ``None`` if deny due to timeout
        """

        author_id = author_id or self.author.id
        view = ConfirmationView(
            timeout=timeout,
            delete_after=delete_after,
            author_id=author_id,
        )
        view.message = await self.send(message, view=view, ephemeral=delete_after)
        await view.wait()
        return view.value

    async def web_get(self, url: str):
        async with self.bot.session.get(url) as resp:
            if resp.status != 200:
                raise FileNotFoundError(resp.status, resp.url)
            return await resp.read()


class GuildContext(Context):
    author: discord.Member
    guild: discord.Guild
    channel: (
        discord.VoiceChannel
        | discord.TextChannel
        | discord.Thread
        | discord.StageChannel
    )
    me: discord.Member
    prefix: str

# pyright: standard

from __future__ import annotations

import datetime
from itertools import batched
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from collections.abc import Callable


BULK_DELETE_LIMIT = datetime.timedelta(days=14)
type MessageBoundary = discord.abc.Snowflake | datetime.datetime


class ChannelPurger:
    """Collect and delete messages from a channel."""

    def __init__(
        self,
        channel: discord.abc.Messageable,
        *,
        before: MessageBoundary | None = None,
        after: MessageBoundary | None = None,
    ) -> None:
        self.channel: discord.abc.Messageable = channel
        self.before: MessageBoundary | None = before
        self.after: MessageBoundary | None = after
        self.deleted: list[discord.Message] = []

    async def _delete_single(self, msg: discord.Message) -> bool:
        """Remove one message and return whether deletion should continue."""

        try:
            await msg.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return False

        self.deleted.append(msg)
        return True

    async def _bulk_delete(self, messages: list[discord.Message]) -> None:
        """Bulk-delete in chunks of 100, falling back to individual deletion."""

        for chunk in batched(messages, 100, strict=False):
            try:
                if len(chunk) == 1:
                    await chunk[0].delete()
                else:
                    await self.channel.delete_messages(chunk)  # type: ignore[attr-defined]
                self.deleted.extend(chunk)
            except discord.HTTPException:
                for msg in chunk:
                    if not await self._delete_single(msg):
                        return

    async def delete_messages(
        self,
        messages: list[discord.Message],
    ) -> list[discord.Message]:
        """Deletes known messages using bulk deletion where possible."""

        now = discord.utils.utcnow()
        bulk_cutoff = now - BULK_DELETE_LIMIT

        bulk_msgs: list[discord.Message] = []
        old_msgs: list[discord.Message] = []
        for message in messages:
            if message.created_at > bulk_cutoff:
                bulk_msgs.append(message)
            else:
                old_msgs.append(message)

        await self._bulk_delete(bulk_msgs)
        for msg in old_msgs:
            if not await self._delete_single(msg):
                break

        return self.deleted

    async def purge(
        self,
        limit: int,
        check: Callable[[discord.Message], bool] = lambda _: True,
    ) -> list[discord.Message]:
        """Collect and delete up to ``limit`` messages matching ``check``."""

        messages: list[discord.Message] = []
        scan_limit = min(limit * 5, 5000)

        async for message in self.channel.history(
            limit=scan_limit,
            before=self.before,
            after=self.after,
        ):
            if check(message):
                messages.append(message)
                if len(messages) >= limit:
                    break

        if not messages:
            return self.deleted

        return await self.delete_messages(messages)

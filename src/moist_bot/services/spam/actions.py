"""Apply Discord punishments, delete tracked bursts, and report incident outcomes."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from itertools import batched
from typing import TYPE_CHECKING

import discord

from .types import ActionOutcome, SpamAction, SpamSettings, SpamVerdict

if TYPE_CHECKING:
    from moist_bot.models.spam import SpamIncident

    from .types import MessageReference


log = logging.getLogger('discord.' + __name__)


def validate_permissions(guild: discord.Guild, settings: SpamSettings) -> None:
    """Check the bot's action permissions and configured log channel.

    Parameters
    ----------
    guild : discord.Guild
        Guild where the checker is being enabled or reconfigured.
    settings : SpamSettings
        Requested punishment, cleanup, and logging options.

    Raises
    ------
    ValueError
        Required moderation permissions or a usable log channel are missing.
    """

    me = guild.me

    # Action permissions are checked independently of channel-specific cleanup access
    if (
        settings.action == SpamAction.TIMEOUT
        and not me.guild_permissions.moderate_members
    ):
        raise ValueError('I need Moderate Members for timeout mode')
    if settings.action == SpamAction.BAN and not me.guild_permissions.ban_members:
        raise ValueError('I need Ban Members for ban mode')
    if (
        settings.delete_burst
        and settings.action != SpamAction.ALERT
        and not me.guild_permissions.manage_messages
    ):
        raise ValueError('I need Manage Messages for burst cleanup')

    # A configured text log channel is required even for alert-only operation
    channel = guild.get_channel(settings.log_channel_id or 0)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError('Set a log channel in this server before enabling spam checks')  # ruff: ignore[type-check-without-type-error] - Invalid configuration
    permissions = channel.permissions_for(me)
    if (
        not permissions.view_channel
        or not permissions.send_messages
        or not permissions.embed_links
    ):
        raise ValueError(
            'I need View Channel, Send Messages, and Embed Links in the log channel'
        )


class SpamActions:
    """Perform Discord I/O after the manager commits a detection and cooldown."""

    async def apply(
        self,
        member: discord.Member,
        verdict: SpamVerdict,
        settings: SpamSettings,
    ) -> ActionOutcome:
        """Apply the configured punishment and clean its tracked burst on success.

        Parameters
        ----------
        member : discord.Member
            Triggering member, already checked against service exemptions.
        verdict : SpamVerdict
            Violated rule, audit reason, and the member's burst references.
        settings : SpamSettings
            Committed action and cleanup configuration.

        Returns
        -------
        ActionOutcome
            Punishment success or failure, plus independent cleanup results.

        Notes
        -----
        Alert mode skips punishment and cleanup. Existing longer timeouts are
        preserved. Punishment failures return immediately without deleting messages.
        """

        if settings.action == SpamAction.ALERT:
            return ActionOutcome(succeeded=True)

        if member.top_role >= member.guild.me.top_role:
            return ActionOutcome(
                succeeded=False, error='Member is above my role hierarchy'
            )

        until = discord.utils.utcnow() + timedelta(seconds=settings.timeout_seconds)

        try:
            if settings.action == SpamAction.TIMEOUT:
                if member.timed_out_until is None or member.timed_out_until < until:
                    await member.timeout(until, reason=verdict.reason)
            else:
                # Only delete tracked burst messages, without deleting unrelated history
                await member.ban(reason=verdict.reason, delete_message_seconds=0)
        except discord.HTTPException as exc:
            log.warning(
                f'Spam punishment failed for {member.id} in {member.guild.id}: {exc}'
            )
            return ActionOutcome(succeeded=False, error=str(exc))

        deleted = 0
        cleanup_error: str | None = None
        if settings.delete_burst:
            deleted, cleanup_error = await self.delete_burst(member, verdict.messages)

        return ActionOutcome(
            succeeded=True, deleted=deleted, cleanup_error=cleanup_error
        )

    async def delete_burst(
        self,
        member: discord.Member,
        references: tuple[MessageReference, ...],
    ) -> tuple[int, str | None]:
        """Delete only the triggering member's tracked messages, grouped by channel.

        Parameters
        ----------
        member : discord.Member
            Member whose messages may be deleted.
        references : tuple[MessageReference, ...]
            Recent message identities retained by the violated rule.

        Returns
        -------
        tuple[int, str or None]
            Successful deletion-request count and any collected cleanup errors.
            Bulk deletion cannot distinguish IDs already deleted by another actor.
        """

        # Filter again at the action boundary to protect other contributors' messages
        grouped: defaultdict[int, list[int]] = defaultdict(list)
        for reference in references:
            if reference.user_id == member.id:
                grouped[reference.channel_id].append(reference.message_id)

        deleted = 0
        errors: list[str] = []

        for channel_id, message_ids in grouped.items():
            # Stored references can outlive a channel or its cached thread parent
            channel = member.guild.get_channel_or_thread(channel_id)
            if not isinstance(
                channel,
                (
                    discord.TextChannel,
                    discord.Thread,
                    discord.VoiceChannel,
                    discord.StageChannel,
                ),
            ):
                errors.append(f'Channel {channel_id} is unavailable')
                continue

            if isinstance(channel, discord.Thread) and channel.parent is None:
                errors.append(f'Parent of thread {channel_id} is unavailable')
                continue

            if not channel.permissions_for(member.guild.me).manage_messages:
                errors.append(f'Missing Manage Messages in channel {channel_id}')
                continue

            # Discord accepts at most 100 IDs per bulk-delete request
            for chunk in batched(dict.fromkeys(message_ids), 100, strict=False):
                count, error = await self._delete_chunk(channel, chunk)
                deleted += count
                if error:
                    errors.append(f'Channel {channel_id}: {error}')

        return deleted, '; '.join(errors) or None

    async def _delete_chunk(
        self,
        channel: discord.TextChannel
        | discord.Thread
        | discord.VoiceChannel
        | discord.StageChannel,
        message_ids: tuple[int, ...],
    ) -> tuple[int, str | None]:
        """Delete a chunk in bulk, falling back to single requests when appropriate.

        Returns
        -------
        tuple[int, str or None]
            Count of successful deletion requests and the first unrecoverable error.

        Notes
        -----
        Permission failures and missing bulk-delete channels stop the chunk.
        Other bulk failures fall back to individual message deletion; a missing
        individual message is already gone and is skipped.
        """

        if len(message_ids) > 1:
            try:
                await channel.delete_messages(
                    [discord.Object(message_id) for message_id in message_ids],
                    reason='Spam burst cleanup',
                )
            except (discord.Forbidden, discord.NotFound) as exc:
                log.warning(f'Spam cleanup failed in {channel.id}: {exc}')
                return 0, str(exc)
            except discord.HTTPException:
                log.warning(
                    f'Bulk spam cleanup failed in {channel.id}; deleting individually'
                )
            else:
                return len(message_ids), None

        # A single message or recoverable bulk failure uses the same fallback path
        deleted = 0

        for message_id in message_ids:
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.NotFound:
                # Missing messages are already cleaned up
                continue
            except discord.HTTPException as exc:
                log.warning(f'Spam cleanup failed in {channel.id}: {exc}')
                return deleted, str(exc)
            else:
                deleted += 1

        return deleted, None

    async def report(
        self, guild: discord.Guild, settings: SpamSettings, incident: SpamIncident
    ) -> None:
        """Send the saved incident outcome to the configured moderation log.

        Parameters
        ----------
        guild : discord.Guild
            Guild where the incident occurred.
        settings : SpamSettings
            Configuration snapshot used when the incident was detected.
        incident : SpamIncident
            Final database record containing punishment and cleanup outcomes.

        Notes
        -----
        Logging failures are reported through Python logging. They do not change
        the already-committed punishment outcome or cause moderation to be retried.
        """

        channel = guild.get_channel(settings.log_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            log.warning(f'Spam log channel unavailable in guild {guild.id}')
            return

        # Present action status and cleanup errors separately for moderator review
        embed = discord.Embed(
            title='Spam detected',
            description=incident.reason,
            colour=discord.Colour.orange(),
        )
        embed.add_field(name='User', value=str(incident.user_id))
        embed.add_field(name='Action', value=f'{incident.action}: {incident.status}')
        embed.add_field(name='Messages deleted', value=str(incident.messages_deleted))
        embed.add_field(name='Channel', value=f'<#{incident.channel_id}>')

        if incident.content_excerpt:
            embed.add_field(
                name='Trigger message', value=incident.content_excerpt, inline=False
            )
        if incident.error:
            embed.add_field(
                name='Action error', value=incident.error[:1000], inline=False
            )
        if incident.cleanup_error:
            embed.add_field(
                name='Cleanup error', value=incident.cleanup_error[:1000], inline=False
            )

        try:
            await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            log.exception(
                f'Failed to send spam incident {incident.id} in guild {guild.id}'
            )

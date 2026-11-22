"""Outbound Discord messaging, behind a tiny interface.

The reports module talks to `ReplySink` and never to discord.py. That keeps the module
testable without a gateway connection and lets the same code run in a dry-run mode.
"""

from __future__ import annotations

from typing import Any, Protocol

import discord
import structlog

log = structlog.get_logger(__name__)


class ReplySink(Protocol):
    async def send(
        self,
        channel_id: int,
        content: str,
        *,
        ephemeral: bool = False,
        reference_message_id: int | None = None,
    ) -> int | None: ...
    async def edit(
        self, channel_id: int, message_id: int, content: str, *, ephemeral: bool = False
    ) -> bool: ...
    async def clear(self, channel_id: int, message_id: int) -> bool: ...


class DiscordReplySink:
    """Posts into channels. Warnings are sent as plain messages, not replies.

    A reply to the report would be threaded under it and harder to notice. A plain
    message that mentions the author is the behaviour the channel expects.

    Channel lookup falls back to a REST fetch. `get_channel` only works for channels
    present in the local cache, and it returned `None` here even though the channel was
    fully reachable, which made every send a silent no-op while callers believed it
    succeeded.
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client
        self._cache: dict[int, Any] = {}
        # Why the last send failed, so a caller can tell the user instead of reporting
        # "nothing was posted". A bool return loses the only piece of information an
        # admin needs: whether it was a permission, a missing channel, or Discord.
        self.last_error: str = ""

    async def _resolve(self, channel_id: int) -> Any:
        channel = self._client.get_channel(channel_id)
        if channel is not None:
            return channel
        cached = self._cache.get(channel_id)
        if cached is not None:
            return cached
        try:
            channel = await self._client.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            log.error("reply_channel_fetch_failed", channel_id=channel_id, error=str(exc))
            self.last_error = f"канал <#{channel_id}> не найден или бот его не видит"
            return None
        self._cache[channel_id] = channel
        log.info("reply_channel_fetched", channel_id=channel_id)
        return channel

    async def send(
        self,
        channel_id: int,
        content: str,
        *,
        ephemeral: bool = False,
        reference_message_id: int | None = None,
    ) -> int | None:
        self.last_error = ""
        channel = await self._resolve(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.error("reply_channel_not_messageable", channel_id=channel_id)
            self.last_error = f"<#{channel_id}> — это не текстовый канал"
            return None
        reference: Any = None
        if reference_message_id is not None:
            getter = getattr(channel, "get_partial_message", None)
            if getter is not None:
                reference = getter(reference_message_id)
        try:
            # `ephemeral` cannot be honoured outside an interaction. A report warning must
            # be visible to the reporter openly, so this flag exists only for interface
            # compatibility and is ignored here.
            message = await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(users=True),
                reference=reference,
                mention_author=False,
            )
        except discord.Forbidden:
            log.error(
                "reply_forbidden",
                channel_id=channel_id,
                hint="the bot lacks View Channel / Send Messages in that channel",
            )
            self.last_error = (
                f"нет доступа к <#{channel_id}>: нужны права «Просматривать канал» и "
                f"«Отправлять сообщения»"
            )
            return None
        except discord.HTTPException as exc:
            log.warning("reply_send_failed", channel_id=channel_id, error=str(exc))
            self.last_error = f"Discord отклонил отправку в <#{channel_id}>: {exc.text or exc}"
            return None
        return message.id

    async def edit(
        self, channel_id: int, message_id: int, content: str, *, ephemeral: bool = False
    ) -> bool:
        channel = await self._resolve(channel_id)
        partial = _partial_message(channel, message_id)
        if partial is None:
            return False
        try:
            await partial.edit(content=content)
            return True
        except discord.NotFound:
            # The warning was deleted by a moderator; reposting is the right move.
            return False
        except discord.HTTPException:
            log.exception("reply_edit_failed", channel_id=channel_id, message_id=message_id)
            return False

    async def clear(self, channel_id: int, message_id: int) -> bool:
        """Delete our own warning once the report is fixed.

        No MANAGE_MESSAGES needed: Discord only requires that permission to delete a
        message the bot did not send itself.
        """
        channel = await self._resolve(channel_id)
        partial = _partial_message(channel, message_id)
        if partial is None:
            return False
        try:
            await partial.delete()
            return True
        except discord.NotFound:
            return True  # already gone: the desired end state
        except discord.Forbidden:
            # Almost always a non-bot-authored message, or a permission change.
            log.error(
                "reply_clear_forbidden",
                channel_id=channel_id,
                message_id=message_id,
                hint="the bot can only delete its own messages without MANAGE_MESSAGES",
            )
            return False
        except discord.HTTPException:
            log.exception("reply_clear_failed", channel_id=channel_id, message_id=message_id)
            return False


def _partial_message(channel: Any, message_id: int) -> Any | None:
    """Get a partial message handle; only channels that hold messages support this."""
    getter = getattr(channel, "get_partial_message", None)
    if getter is None:
        log.error("channel_cannot_edit_messages", channel_id=getattr(channel, "id", None))
        return None
    return getter(message_id)


async def can_post_to(channel: Any) -> str:
    """Can the bot actually post here? Returns "" when it can, else the reason.

    Probed by sending a throwaway message and deleting it, rather than computing
    permissions: `permissions_for` needs the Members intent and gets channel overwrites
    wrong, so a real send is the only answer that cannot be wrong. The probe deletes
    itself, so the channel is left as it was.

    One implementation, used by `/dsbot report-target` (before saving a destination) and
    by the scheduled close (before counting a period). These were two copies of the same
    text, and two copies of an error message drift until one of them is wrong.
    """
    if channel is None:
        return "канал не найден или бот его не видит"
    channel_id = getattr(channel, "id", None)
    send = getattr(channel, "send", None)
    if send is None:
        return f"<#{channel_id}> — это не текстовый канал, писать в него нельзя"
    try:
        probe = await send(
            "dsbot: проверка прав публикации (удалится само)",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        return (
            f"нет доступа к <#{channel_id}>: боту нужно право «Просматривать канал» "
            f"и «Отправлять сообщения»"
        )
    except discord.HTTPException as exc:
        return f"Discord отклонил проверку <#{channel_id}>: {exc.text or exc}"
    try:
        await probe.delete()
    except Exception:
        # Leaving one probe behind is harmless; the permission was proven.
        log.warning("probe_not_deleted", channel_id=channel_id)
    return ""


class RecordingReplySink:
    """Used by tests and `--dry-run`. Records instead of sending."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.references: list[int | None] = []
        self.edited: list[tuple[int, int, str]] = []
        self.cleared: list[tuple[int, int]] = []
        # The ids handed back to the caller. The live sink returns a real message id and
        # callers store it, so a fake that only records the body hides bugs about ids.
        self.sent_ids: list[int] = []
        self._next_id = 1000

    async def send(
        self,
        channel_id: int,
        content: str,
        *,
        ephemeral: bool = False,
        reference_message_id: int | None = None,
    ) -> int | None:
        self.sent.append((channel_id, content))
        self.references.append(reference_message_id)
        self._next_id += 1
        self.sent_ids.append(self._next_id)
        return self._next_id

    async def edit(
        self, channel_id: int, message_id: int, content: str, *, ephemeral: bool = False
    ) -> bool:
        self.edited.append((channel_id, message_id, content))
        return True

    async def clear(self, channel_id: int, message_id: int) -> bool:
        self.cleared.append((channel_id, message_id))
        return True

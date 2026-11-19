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
        self, channel_id: int, content: str, *, ephemeral: bool = False
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

    async def _resolve(self, channel_id: int) -> Any:
        channel = self._client.get_channel(channel_id)
        if channel is not None:
            return channel
        cached = self._cache.get(channel_id)
        if cached is not None:
            return cached
        try:
            channel = await self._client.fetch_channel(channel_id)
        except discord.HTTPException:
            log.error("reply_channel_fetch_failed", channel_id=channel_id)
            return None
        self._cache[channel_id] = channel
        log.info("reply_channel_fetched", channel_id=channel_id)
        return channel

    async def send(self, channel_id: int, content: str, *, ephemeral: bool = False) -> int | None:
        channel = await self._resolve(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.error("reply_channel_not_messageable", channel_id=channel_id)
            return None
        try:
            # `ephemeral` cannot be honoured outside an interaction. Warning a report in
            # a channel must be visible to the reporter openly, so this flag is accepted
            # for interface compatibility and ignored here.
            message = await channel.send(
                content, allowed_mentions=discord.AllowedMentions(users=True)
            )
        except discord.Forbidden:
            log.error("reply_forbidden", channel_id=channel_id)
            return None
        except discord.HTTPException:
            log.exception("reply_send_failed", channel_id=channel_id)
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
        """Delete our own warning once the report is fixed."""
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
            log.error("reply_clear_forbidden", channel_id=channel_id)
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


class RecordingReplySink:
    """Used by tests and `--dry-run`. Records instead of sending."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.cleared: list[tuple[int, int]] = []
        self._next_id = 1000

    async def send(self, channel_id: int, content: str, *, ephemeral: bool = False) -> int | None:
        self.sent.append((channel_id, content))
        self._next_id += 1
        return self._next_id

    async def edit(
        self, channel_id: int, message_id: int, content: str, *, ephemeral: bool = False
    ) -> bool:
        self.edited.append((channel_id, message_id, content))
        return True

    async def clear(self, channel_id: int, message_id: int) -> bool:
        self.cleared.append((channel_id, message_id))
        return True

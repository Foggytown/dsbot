"""Outbound Discord messaging, behind a tiny interface.

The reports module talks to `ReplySink` and never to discord.py. That keeps the module
testable without a gateway connection and lets the same code run in a dry-run mode.
"""

from __future__ import annotations

from typing import Protocol

import discord
import structlog

log = structlog.get_logger(__name__)


class ReplySink(Protocol):
    async def send(self, channel_id: int, content: str) -> int | None: ...
    async def edit(self, channel_id: int, message_id: int, content: str) -> bool: ...
    async def clear(self, channel_id: int, message_id: int) -> bool: ...


class DiscordReplySink:
    """Posts into channels. Warnings are sent as plain messages, not replies.

    A reply to the report would be threaded under it and harder to notice. A plain
    message that mentions the author is the behaviour the channel expects.
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def send(self, channel_id: int, content: str) -> int | None:
        channel = self._client.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("reply_channel_missing", channel_id=channel_id)
            return None
        try:
            message = await channel.send(
                content, allowed_mentions=discord.AllowedMentions(users=True)
            )
        except discord.HTTPException:
            log.exception("reply_send_failed", channel_id=channel_id)
            return None
        return message.id

    async def edit(self, channel_id: int, message_id: int, content: str) -> bool:
        channel = self._client.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return False
        try:
            partial = channel.get_partial_message(message_id)
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
        channel = self._client.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return False
        try:
            partial = channel.get_partial_message(message_id)
            await partial.delete()
            return True
        except discord.NotFound:
            return True  # already gone: the desired end state
        except discord.HTTPException:
            log.exception("reply_clear_failed", channel_id=channel_id, message_id=message_id)
            return False


class RecordingReplySink:
    """Used by tests and `--dry-run`. Records instead of sending."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.cleared: list[tuple[int, int]] = []
        self._next_id = 1000

    async def send(self, channel_id: int, content: str) -> int | None:
        self.sent.append((channel_id, content))
        self._next_id += 1
        return self._next_id

    async def edit(self, channel_id: int, message_id: int, content: str) -> bool:
        self.edited.append((channel_id, message_id, content))
        return True

    async def clear(self, channel_id: int, message_id: int) -> bool:
        self.cleared.append((channel_id, message_id))
        return True

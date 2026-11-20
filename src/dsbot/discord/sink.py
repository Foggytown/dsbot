"""Discord objects -> internal events.

This is the only module that imports discord.py for ingestion. Nothing downstream
sees a discord.Message, so backfill, replay and tests all produce identical events.

Note on cross-server reactions: when a user reacts from a different guild, discord.py
hands us a `User`, not a `Member`, so `.roles` is unavailable. We therefore never
read ranks here; the rank resolver looks them up from the user's home guild.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

from dsbot.core.events import (
    AttachmentRef,
    EmbedRef,
    EmojiRef,
    MessageCreated,
    MessageDeleted,
    MessageEdited,
    MessageSnapshot,
    ReactionAdded,
    ReactionCleared,
    ReactionRemoved,
)


def emoji_ref(emoji: Any) -> EmojiRef:
    """Normalise a discord.PartialEmoji / str / Emoji into EmojiRef."""
    if isinstance(emoji, str):
        return EmojiRef(id=None, name=emoji, animated=False)
    return EmojiRef(
        id=getattr(emoji, "id", None),
        name=getattr(emoji, "name", None),
        animated=bool(getattr(emoji, "animated", False)),
    )


def _attachment_ref(attachment: discord.Attachment) -> AttachmentRef:
    return AttachmentRef(
        id=attachment.id,
        filename=attachment.filename,
        url=attachment.url,
        proxy_url=attachment.proxy_url,
        content_type=attachment.content_type,
        size=attachment.size,
        width=getattr(attachment, "width", None),
        height=getattr(attachment, "height", None),
    )


def _embed_ref(embed: discord.Embed) -> EmbedRef:
    return EmbedRef(url=embed.url, title=embed.title, description=embed.description)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def message_snapshot(message: discord.Message) -> MessageSnapshot:
    author = message.author
    return MessageSnapshot(
        message_id=message.id,
        channel_id=message.channel.id,
        guild_id=message.guild.id if message.guild else None,
        author_id=author.id,
        author_name=getattr(author, "name", "") or str(author),
        author_display_name=getattr(author, "display_name", None),
        author_is_bot=bool(getattr(author, "bot", False)),
        content=message.content or "",
        created_at=_as_utc(message.created_at),
        edited_at=_as_utc(message.edited_at) if message.edited_at else None,
        attachments=tuple(_attachment_ref(a) for a in message.attachments),
        embeds=tuple(_embed_ref(e) for e in message.embeds),
        referenced_message_id=getattr(message.reference, "message_id", None),
        thread_id=message.channel.id if isinstance(message.channel, discord.Thread) else None,
    )


def message_created(message: discord.Message) -> MessageCreated:
    return MessageCreated(message=message_snapshot(message))


def message_edited(before: discord.Message | None, after: discord.Message) -> MessageEdited:
    return MessageEdited(
        message=message_snapshot(after),
        previous_content=before.content if before is not None else None,
    )


def message_deleted(payload: discord.RawMessageDeleteEvent) -> MessageDeleted:
    return MessageDeleted(
        message_id=payload.message_id,
        channel_id=payload.channel_id,
        guild_id=payload.guild_id,
    )


def reaction_added(payload: discord.RawReactionActionEvent) -> ReactionAdded:
    return ReactionAdded(
        channel_id=payload.channel_id,
        guild_id=payload.guild_id,
        message_id=payload.message_id,
        user_id=payload.user_id,
        emoji=emoji_ref(payload.emoji),
        message_author_id=getattr(payload, "message_author_id", None),
        reacted_at=datetime.now(UTC),
    )


def reaction_removed(payload: discord.RawReactionActionEvent) -> ReactionRemoved:
    return ReactionRemoved(
        channel_id=payload.channel_id,
        guild_id=payload.guild_id,
        message_id=payload.message_id,
        user_id=payload.user_id,
        emoji=emoji_ref(payload.emoji),
        reacted_at=datetime.now(UTC),
    )


def reaction_cleared(payload: discord.RawReactionClearEmojiEvent) -> ReactionCleared:
    return ReactionCleared(
        channel_id=payload.channel_id,
        guild_id=payload.guild_id,
        message_id=payload.message_id,
        emoji=emoji_ref(payload.emoji),
        cleared_at=datetime.now(UTC),
    )

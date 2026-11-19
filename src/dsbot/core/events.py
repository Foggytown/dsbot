"""Typed, storage-independent events.

Discord objects are never passed past the sink layer. Everything downstream speaks
these dataclasses, which makes replay, backfill and tests share exactly one code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class EmojiRef:
    """A reaction emoji, normalised.

    Custom emoji are identified by id (names change, ids do not). Unicode emoji are
    identified by their codepoint sequence, which is stable.
    """

    id: int | None
    name: str | None
    animated: bool = False

    @property
    def key(self) -> str:
        if self.id is not None:
            return f"custom:{self.id}"
        return f"unicode:{self.name or ''}"

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "animated": self.animated}


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """Attachment metadata only. Files are never downloaded in phase 1."""

    id: int
    filename: str
    url: str
    proxy_url: str | None = None
    content_type: str | None = None
    size: int = 0
    width: int | None = None
    height: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "url": self.url,
            "proxy_url": self.proxy_url,
            "content_type": self.content_type,
            "size": self.size,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class EmbedRef:
    url: str | None = None
    title: str | None = None
    description: str | None = None
    # Discord renders linked previews as embeds; a report citing a message link
    # produces one. Keep the fields we can reason about later.

    def to_payload(self) -> dict[str, Any]:
        return {"url": self.url, "title": self.title, "description": self.description}


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    """Structural facts about a message, independent of any Discord object."""

    message_id: int
    channel_id: int
    guild_id: int | None
    author_id: int
    author_name: str
    author_display_name: str | None
    author_is_bot: bool
    content: str
    created_at: datetime
    edited_at: datetime | None = None
    attachments: tuple[AttachmentRef, ...] = ()
    embeds: tuple[EmbedRef, ...] = ()
    referenced_message_id: int | None = None
    thread_id: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "author_display_name": self.author_display_name,
            "author_is_bot": self.author_is_bot,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "edited_at": self.edited_at.isoformat() if self.edited_at else None,
            "attachments": [a.to_payload() for a in self.attachments],
            "embeds": [e.to_payload() for e in self.embeds],
            "referenced_message_id": self.referenced_message_id,
            "thread_id": self.thread_id,
        }


# --- events ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessageCreated:
    kind: Literal["message_created"] = "message_created"
    message: MessageSnapshot = None  # type: ignore[assignment]

    @property
    def external_id(self) -> str:
        m = self.message
        return f"msg.create:{m.guild_id}:{m.channel_id}:{m.message_id}"


@dataclass(frozen=True, slots=True)
class MessageEdited:
    kind: Literal["message_edited"] = "message_edited"
    message: MessageSnapshot = None  # type: ignore[assignment]
    previous_content: str | None = None

    @property
    def external_id(self) -> str:
        m = self.message
        stamp = m.edited_at.isoformat() if m.edited_at else "unknown"
        return f"msg.edit:{m.guild_id}:{m.channel_id}:{m.message_id}:{stamp}"


@dataclass(frozen=True, slots=True)
class MessageDeleted:
    kind: Literal["message_deleted"] = "message_deleted"
    message_id: int = 0
    channel_id: int = 0
    guild_id: int | None = None

    @property
    def external_id(self) -> str:
        return f"msg.delete:{self.guild_id}:{self.channel_id}:{self.message_id}"


@dataclass(frozen=True, slots=True)
class ReactionAdded:
    kind: Literal["reaction_added"] = "reaction_added"
    channel_id: int = 0
    guild_id: int | None = None
    message_id: int = 0
    user_id: int = 0
    emoji: EmojiRef = None  # type: ignore[assignment]
    message_author_id: int | None = None
    reacted_at: datetime | None = None

    @property
    def external_id(self) -> str:
        return f"react.add:{self.channel_id}:{self.message_id}:{self.emoji.key}:{self.user_id}"


@dataclass(frozen=True, slots=True)
class ReactionRemoved:
    kind: Literal["reaction_removed"] = "reaction_removed"
    channel_id: int = 0
    guild_id: int | None = None
    message_id: int = 0
    user_id: int = 0
    emoji: EmojiRef = None  # type: ignore[assignment]
    reacted_at: datetime | None = None

    @property
    def external_id(self) -> str:
        stamp = self.reacted_at.isoformat() if self.reacted_at else "unknown"
        return (
            f"react.remove:{self.channel_id}:{self.message_id}"
            f":{self.emoji.key}:{self.user_id}:{stamp}"
        )


@dataclass(frozen=True, slots=True)
class ReactionCleared:
    """All reactions of one emoji were removed from a message."""

    kind: Literal["reaction_cleared"] = "reaction_cleared"
    channel_id: int = 0
    guild_id: int | None = None
    message_id: int = 0
    emoji: EmojiRef = None  # type: ignore[assignment]

    @property
    def external_id(self) -> str:
        return f"react.clear:{self.channel_id}:{self.message_id}:{self.emoji.key}"


Event = (
    MessageCreated
    | MessageEdited
    | MessageDeleted
    | ReactionAdded
    | ReactionRemoved
    | ReactionCleared
)


def event_external_id(event: Event) -> str:
    return event.external_id


def event_payload(event: Event) -> dict[str, Any]:
    """Serialise an event for the append-only raw log."""
    payload: dict[str, Any] = {"kind": event.kind}
    match event:
        case MessageCreated(message=m):
            payload["message"] = m.to_payload()
        case MessageEdited(message=m, previous_content=prev):
            payload["message"] = m.to_payload()
            payload["previous_content"] = prev
        case MessageDeleted():
            payload |= {
                "message_id": event.message_id,
                "channel_id": event.channel_id,
                "guild_id": event.guild_id,
            }
        case ReactionAdded() | ReactionRemoved():
            payload |= {
                "channel_id": event.channel_id,
                "guild_id": event.guild_id,
                "message_id": event.message_id,
                "user_id": event.user_id,
                "emoji": event.emoji.to_payload(),
                "message_author_id": getattr(event, "message_author_id", None),
                "reacted_at": event.reacted_at.isoformat() if event.reacted_at else None,
            }
        case ReactionCleared():
            payload |= {
                "channel_id": event.channel_id,
                "guild_id": event.guild_id,
                "message_id": event.message_id,
                "emoji": event.emoji.to_payload(),
            }
    return payload


def event_channel_id(event: Event) -> int | None:
    """Ordering key. Events for one channel must be processed in arrival order."""
    match event:
        case MessageCreated(message=m) | MessageEdited(message=m):
            return m.channel_id
        case MessageDeleted(channel_id=cid):
            return cid
        case (
            ReactionAdded(channel_id=cid)
            | ReactionRemoved(channel_id=cid)
            | ReactionCleared(channel_id=cid)
        ):
            return cid
    return None


def event_message_id(event: Event) -> int | None:
    match event:
        case MessageCreated(message=m) | MessageEdited(message=m):
            return m.message_id
        case MessageDeleted(message_id=mid):
            return mid
        case (
            ReactionAdded(message_id=mid)
            | ReactionRemoved(message_id=mid)
            | ReactionCleared(message_id=mid)
        ):
            return mid
    return None

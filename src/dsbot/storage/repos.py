"""Repository functions. SQL lives here and nowhere else.

Handlers and modules call these; they never build queries themselves. That is what
makes the SQLite -> Postgres move a single-file concern.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsbot.core.events import (
    Event,
    MessageCreated,
    MessageEdited,
    event_channel_id,
    event_message_id,
    event_payload,
)
from dsbot.storage.models import (
    Channel,
    ChannelBinding,
    Guild,
    Message,
    MessageReaction,
    RawEvent,
    Report,
    UserRankCache,
)

# --- raw events ------------------------------------------------------------


async def append_raw_event(
    session: AsyncSession,
    event: Event,
    *,
    source: str = "gateway",
    occurred_at: datetime | None = None,
) -> bool:
    """Append to the raw log. Returns False when the event was already stored."""
    external_id = event.external_id
    existing = await session.scalar(
        select(RawEvent.id).where(RawEvent.external_id == external_id).limit(1)
    )
    if existing is not None:
        return False

    session.add(
        RawEvent(
            external_id=external_id,
            kind=event.kind,
            channel_id=event_channel_id(event),
            guild_id=_event_guild(event),
            message_id=event_message_id(event),
            occurred_at=occurred_at,
            source=source,
            payload=_jsonable(event_payload(event)),
        )
    )
    return True


def _event_guild(event: Event) -> int | None:
    match event:
        case MessageCreated(message=m) | MessageEdited(message=m):
            return m.guild_id
        case _:
            return event.guild_id


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


# --- guilds / channels -----------------------------------------------------


async def upsert_guild(
    session: AsyncSession, guild_id: int, name: str = "", *, is_report_guild: bool | None = None
) -> None:
    guild = await session.get(Guild, guild_id)
    if guild is None:
        guild = Guild(
            id=guild_id,
            name=name,
            is_report_guild=bool(is_report_guild),
            is_rank_source=False,
        )
        session.add(guild)
        return
    if name and guild.name != name:
        guild.name = name
    if is_report_guild is not None:
        guild.is_report_guild = is_report_guild


async def upsert_channel(
    session: AsyncSession,
    channel_id: int,
    guild_id: int,
    name: str = "",
    *,
    parent_id: int | None = None,
    kind: str = "text",
) -> None:
    channel = await session.get(Channel, channel_id)
    if channel is None:
        session.add(
            Channel(id=channel_id, guild_id=guild_id, name=name, parent_id=parent_id, kind=kind)
        )
        return
    if name and channel.name != name:
        channel.name = name
    if parent_id is not None and channel.parent_id != parent_id:
        channel.parent_id = parent_id
    if kind and channel.kind != kind:
        channel.kind = kind


async def get_binding_by_channel(session: AsyncSession, channel_id: int) -> ChannelBinding | None:
    return await session.scalar(
        select(ChannelBinding).where(
            ChannelBinding.channel_id == channel_id, ChannelBinding.enabled.is_(True)
        )
    )


async def list_enabled_bindings(session: AsyncSession) -> Sequence[ChannelBinding]:
    result = await session.scalars(select(ChannelBinding).where(ChannelBinding.enabled.is_(True)))
    return result.all()


# --- messages --------------------------------------------------------------


async def get_message(session: AsyncSession, channel_id: int, message_id: int) -> Message | None:
    return await session.scalar(
        select(Message).where(Message.channel_id == channel_id, Message.message_id == message_id)
    )


async def get_message_by_id(session: AsyncSession, message_id: int) -> Message | None:
    return await session.scalar(select(Message).where(Message.message_id == message_id))


async def upsert_message(session: AsyncSession, snapshot: Any) -> Message:
    """Idempotent by (channel_id, message_id). Backfill and live ingest converge."""
    message = await get_message(session, snapshot.channel_id, snapshot.message_id)
    attachments = [a.to_payload() for a in snapshot.attachments]
    embeds = [e.to_payload() for e in snapshot.embeds]

    if message is None:
        message = Message(
            message_id=snapshot.message_id,
            channel_id=snapshot.channel_id,
            guild_id=snapshot.guild_id,
            author_id=snapshot.author_id,
            author_name=snapshot.author_name,
            author_display_name=snapshot.author_display_name,
            author_is_bot=snapshot.author_is_bot,
            content=snapshot.content,
            created_at=snapshot.created_at,
            edited_at=snapshot.edited_at,
            referenced_message_id=snapshot.referenced_message_id,
            thread_id=snapshot.thread_id,
            attachments=attachments,
            embeds=embeds,
        )
        session.add(message)
        return message

    message.content = snapshot.content
    message.author_name = snapshot.author_name
    message.author_display_name = snapshot.author_display_name
    message.edited_at = snapshot.edited_at
    message.attachments = attachments
    message.embeds = embeds
    message.referenced_message_id = snapshot.referenced_message_id
    return message


async def mark_message_deleted(
    session: AsyncSession, channel_id: int, message_id: int, when: datetime | None = None
) -> None:
    message = await get_message(session, channel_id, message_id)
    if message is not None:
        message.deleted_at = when or datetime.now(UTC)


async def list_messages_between(
    session: AsyncSession,
    channel_id: int,
    start: datetime,
    end: datetime,
    *,
    author_id: int | None = None,
) -> Sequence[Message]:
    stmt = select(Message).where(
        Message.channel_id == channel_id,
        Message.created_at >= start,
        Message.created_at < end,
        Message.deleted_at.is_(None),
    )
    if author_id is not None:
        stmt = stmt.where(Message.author_id == author_id)
    return (await session.scalars(stmt.order_by(Message.created_at))).all()


# --- reactions -------------------------------------------------------------


async def add_reaction(
    session: AsyncSession,
    *,
    message_id: int,
    channel_id: int,
    guild_id: int | None,
    emoji_key: str,
    emoji_id: int | None,
    emoji_name: str | None,
    user_id: int,
    tier_ordinal: int | None = None,
    tier_label: str | None = None,
) -> None:
    existing = await session.scalar(
        select(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.emoji_key == emoji_key,
            MessageReaction.user_id == user_id,
        )
    )
    if existing is not None:
        existing.tier_ordinal = tier_ordinal
        existing.tier_label = tier_label
        return
    # Flush the lookup's pending state before the INSERT, so a DELETE earlier in the
    # same transaction cannot be resurrected by a stale identity-map entry.
    await session.flush()
    session.add(
        MessageReaction(
            message_id=message_id,
            channel_id=channel_id,
            guild_id=guild_id,
            emoji_key=emoji_key,
            emoji_id=emoji_id,
            emoji_name=emoji_name,
            user_id=user_id,
            tier_ordinal=tier_ordinal,
            tier_label=tier_label,
        )
    )


async def remove_reaction(
    session: AsyncSession, *, message_id: int, emoji_key: str, user_id: int
) -> None:
    await session.execute(
        delete(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.emoji_key == emoji_key,
            MessageReaction.user_id == user_id,
        )
    )


async def remove_reaction_emoji(session: AsyncSession, *, message_id: int, emoji_key: str) -> None:
    await session.execute(
        delete(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.emoji_key == emoji_key,
        )
    )


async def list_reactions(session: AsyncSession, message_id: int) -> Sequence[MessageReaction]:
    return (
        await session.scalars(
            select(MessageReaction)
            .where(MessageReaction.message_id == message_id)
            .order_by(MessageReaction.added_at)
        )
    ).all()


# --- reports ---------------------------------------------------------------


async def get_report_by_message(
    session: AsyncSession, channel_id: int, message_id: int
) -> Report | None:
    return await session.scalar(
        select(Report).where(Report.channel_id == channel_id, Report.message_id == message_id)
    )


async def upsert_report(
    session: AsyncSession,
    *,
    channel_id: int,
    message_id: int,
    guild_id: int | None,
    author_user_id: int,
    created_at: datetime,
    binding_id: int | None = None,
    schema_id: int | None = None,
    schema_key: str | None = None,
) -> Report:
    report = await get_report_by_message(session, channel_id, message_id)
    if report is None:
        report = Report(
            channel_id=channel_id,
            message_id=message_id,
            guild_id=guild_id,
            author_user_id=author_user_id,
            created_at=created_at,
            binding_id=binding_id,
            schema_id=schema_id,
            schema_key=schema_key,
        )
        session.add(report)
        await session.flush()
    return report


async def delete_report(session: AsyncSession, report_id: int) -> None:
    await session.execute(delete(Report).where(Report.id == report_id))


async def count_reports(
    session: AsyncSession,
    *,
    channel_id: int,
    start: datetime,
    end: datetime,
    statuses: Iterable[str] | None = None,
) -> int:
    stmt = select(func.count(Report.id)).where(
        Report.channel_id == channel_id,
        Report.created_at >= start,
        Report.created_at < end,
    )
    if statuses is not None:
        stmt = stmt.where(Report.status.in_(list(statuses)))
    return int(await session.scalar(stmt) or 0)


# --- rank cache ------------------------------------------------------------


async def get_cached_rank(
    session: AsyncSession, guild_id: int, user_id: int
) -> UserRankCache | None:
    return await session.scalar(
        select(UserRankCache).where(
            UserRankCache.guild_id == guild_id, UserRankCache.user_id == user_id
        )
    )


async def put_cached_rank(
    session: AsyncSession,
    *,
    guild_id: int,
    user_id: int,
    ladder_id: int | None,
    tier_id: int | None,
    tier_ordinal: int | None,
    source: str = "gateway",
) -> None:
    row = await get_cached_rank(session, guild_id, user_id)
    if row is None:
        session.add(
            UserRankCache(
                guild_id=guild_id,
                user_id=user_id,
                ladder_id=ladder_id,
                tier_id=tier_id,
                tier_ordinal=tier_ordinal,
                source=source,
            )
        )
        return
    row.ladder_id = ladder_id
    row.tier_id = tier_id
    row.tier_ordinal = tier_ordinal
    row.source = source
    row.resolved_at = datetime.now(UTC)

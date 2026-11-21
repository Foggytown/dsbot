"""Repository functions. SQL lives here and nowhere else.

Handlers and modules call these; they never build queries themselves. That is what
makes the SQLite -> Postgres move a single-file concern.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
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
    BindingSchema,
    ChannelBinding,
    FormSchema,
    Message,
    MessageReaction,
    QuotaPolicy,
    RankTier,
    RawEvent,
    Report,
    ReportField,
    ReportVerdictHistory,
    RosterMember,
    UserRankCache,
    ValidationMessage,
    WeeklyResult,
    WeeklyRun,
)

# --- raw events ------------------------------------------------------------


async def append_raw_event(
    session: AsyncSession,
    event: Event,
    *,
    source: str = "gateway",
    occurred_at: datetime | None = None,
) -> bool:
    """Append to the raw log. Returns False when the event was already stored.

    Implemented as an atomic insert-or-ignore rather than check-then-insert. Two bot
    processes running at once, or a backfill racing live traffic, would otherwise both
    pass the existence check and the loser would crash on the unique constraint. That
    actually happened: a stray `dsbot run` left running made message handling die
    intermittently with `UNIQUE constraint failed: raw_events.external_id`.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = (
        sqlite_insert(RawEvent)
        .values(
            external_id=event.external_id,
            kind=event.kind,
            channel_id=event_channel_id(event),
            guild_id=_event_guild(event),
            message_id=event_message_id(event),
            occurred_at=occurred_at,
            source=source,
            payload=_jsonable(event_payload(event)),
        )
        .on_conflict_do_nothing(index_elements=["external_id"])
        .returning(RawEvent.id)
    )
    inserted = await session.scalar(stmt)
    return inserted is not None


def _event_guild(event: Event) -> int | None:
    match event:
        case MessageCreated(message=m) | MessageEdited(message=m):
            return m.guild_id
        case _:
            return event.guild_id


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


# --- guilds / channels -----------------------------------------------------


async def get_binding_by_channel(session: AsyncSession, channel_id: int) -> ChannelBinding | None:
    return await session.scalar(
        select(ChannelBinding).where(
            ChannelBinding.channel_id == channel_id, ChannelBinding.enabled.is_(True)
        )
    )


async def get_any_binding(session: AsyncSession, channel_id: int) -> ChannelBinding | None:
    """Binding for a channel regardless of whether it is enabled.

    Management commands need to see disabled channels too, otherwise `enable` could
    never find the row it is meant to re-enable.
    """
    return await session.scalar(
        select(ChannelBinding).where(ChannelBinding.channel_id == channel_id)
    )


async def list_enabled_bindings(session: AsyncSession) -> Sequence[ChannelBinding]:
    result = await session.scalars(select(ChannelBinding).where(ChannelBinding.enabled.is_(True)))
    return result.all()


# --- messages --------------------------------------------------------------


async def get_message(session: AsyncSession, channel_id: int, message_id: int) -> Message | None:
    return await session.scalar(
        select(Message).where(Message.channel_id == channel_id, Message.message_id == message_id)
    )


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
    tier_key: str | None = None,
    tier_ordinal: int | None = None,
    tier_label: str | None = None,
    can_review: bool | None = None,
) -> None:
    existing = await session.scalar(
        select(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.emoji_key == emoji_key,
            MessageReaction.user_id == user_id,
        )
    )
    if existing is not None:
        existing.tier_key = tier_key
        existing.tier_ordinal = tier_ordinal
        existing.tier_label = tier_label
        existing.can_review = can_review
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
            tier_key=tier_key,
            tier_ordinal=tier_ordinal,
            tier_label=tier_label,
            can_review=can_review,
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


async def get_report_by_message_id(session: AsyncSession, message_id: int) -> Report | None:
    """Look a report up by message alone.

    Reaction events carry the message id and the channel, but a channel-wide lookup
    keeps the verdict path independent of how the event was routed.
    """
    return await session.scalar(select(Report).where(Report.message_id == message_id))


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


async def list_pending_reports(
    session: AsyncSession,
    *,
    channel_id: int,
    author_id: int,
    after: datetime,
    before: datetime,
) -> Sequence[Report]:
    """Reports still waiting for evidence: deferred, or invalid without a warning yet.

    Chat never enters this set because it is stored as `ignored`.
    """
    stmt = (
        select(Report)
        .where(
            Report.channel_id == channel_id,
            Report.author_user_id == author_id,
            Report.status.in_(("deferred", "invalid")),
            Report.created_at >= after,
            Report.created_at <= before,
        )
        .order_by(Report.created_at.desc())
    )
    return (await session.scalars(stmt)).all()


# --- rank cache ------------------------------------------------------------
async def get_cached_rank(
    session: AsyncSession, guild_id: int, user_id: int
) -> UserRankCache | None:
    return await session.scalar(
        select(UserRankCache).where(
            UserRankCache.guild_id == guild_id, UserRankCache.user_id == user_id
        )
    )


async def replace_report_fields(
    session: AsyncSession, report_id: int, fields: list[dict[str, Any]]
) -> None:
    """Rewrite a report's fields wholesale.

    Re-parsing is always a full replacement: a report is derived state, and partial
    updates would leave stale fields from a previous interpretation.
    """
    await session.execute(delete(ReportField).where(ReportField.report_id == report_id))
    for row in fields:
        session.add(ReportField(report_id=report_id, **row))


async def get_validation_message(
    session: AsyncSession, report_id: int, kind: str = "warning"
) -> ValidationMessage | None:
    return await session.scalar(
        select(ValidationMessage).where(
            ValidationMessage.report_id == report_id, ValidationMessage.kind == kind
        )
    )


async def put_validation_message(
    session: AsyncSession,
    *,
    report_id: int,
    channel_id: int,
    message_id: int,
    issue_codes: list[str],
    kind: str = "warning",
) -> None:
    existing = await get_validation_message(session, report_id, kind)
    if existing is None:
        session.add(
            ValidationMessage(
                report_id=report_id,
                kind=kind,
                channel_id=channel_id,
                message_id=message_id,
                issue_codes=issue_codes,
            )
        )
        return
    existing.channel_id = channel_id
    existing.message_id = message_id
    existing.issue_codes = issue_codes


async def delete_validation_message(
    session: AsyncSession, report_id: int, kind: str = "warning"
) -> None:
    await session.execute(
        delete(ValidationMessage).where(
            ValidationMessage.report_id == report_id, ValidationMessage.kind == kind
        )
    )


async def get_schemas_for_binding(session: AsyncSession, binding_id: int) -> Sequence[FormSchema]:
    stmt = (
        select(FormSchema)
        .join(BindingSchema, BindingSchema.schema_id == FormSchema.id)
        .where(BindingSchema.binding_id == binding_id)
        .order_by(BindingSchema.priority)
    )
    return (await session.scalars(stmt)).all()


async def list_reports_for_binding_between(
    session: AsyncSession,
    *,
    binding_id: int,
    start: datetime,
    end: datetime,
) -> Sequence[Report]:
    """Every real report in one period.

    Chat is excluded: only messages that matched a form are reports. `deferred` is a
    retired status kept for old rows and must not be counted either.
    """
    stmt = (
        select(Report)
        .where(
            Report.binding_id == binding_id,
            Report.created_at >= start,
            Report.created_at < end,
            Report.status.in_(("valid", "invalid", "received", "approved", "rejected")),
        )
        .order_by(Report.created_at)
    )
    return (await session.scalars(stmt)).all()


async def list_reactions_for_messages(
    session: AsyncSession, message_ids: Sequence[int]
) -> Sequence[MessageReaction]:
    """All stored reactions for a set of messages, in one query.

    Chunked so a wide period cannot exceed SQLite's bound-parameter limit.
    """
    if not message_ids:
        return []
    rows: list[MessageReaction] = []
    chunk = 500
    for offset in range(0, len(message_ids), chunk):
        part = list(message_ids[offset : offset + chunk])
        result = await session.scalars(
            select(MessageReaction)
            .where(MessageReaction.message_id.in_(part))
            .order_by(MessageReaction.message_id, MessageReaction.added_at)
        )
        rows.extend(result.all())
    return rows


async def put_report_verdict(
    session: AsyncSession,
    *,
    report_id: int,
    verdict: str,
    decided_by_user_id: int | None,
    tier_ordinal: int | None,
    decided_at: datetime | None,
    previous_verdict: str | None,
    reason: str = "reaction",
) -> None:
    """Store the report's current verdict, appending history only on a real change.

    History is a log, not a mirror: writing a row per recomputation would bury the
    meaningful transitions under thousands of no-op entries.
    """
    report = await session.get(Report, report_id)
    if report is None:
        return
    changed = report.verdict != verdict
    report.verdict = verdict
    report.verdict_by_user_id = decided_by_user_id
    report.verdict_tier_ordinal = tier_ordinal
    report.verdict_decided_at = decided_at
    if changed or previous_verdict != verdict:
        session.add(
            ReportVerdictHistory(
                report_id=report_id,
                verdict=verdict,
                decided_by_user_id=decided_by_user_id,
                tier_ordinal=tier_ordinal,
                reason=reason,
            )
        )


async def list_report_verdict_history(
    session: AsyncSession, report_id: int
) -> Sequence[ReportVerdictHistory]:
    return (
        await session.scalars(
            select(ReportVerdictHistory)
            .where(ReportVerdictHistory.report_id == report_id)
            .order_by(ReportVerdictHistory.recorded_at)
        )
    ).all()


# --- phase 3: roster and results -------------------------------------------


async def replace_roster(
    session: AsyncSession, ladder_id: int, members: Sequence[dict[str, Any]]
) -> int:
    """Replace the roster of one ladder with a freshly scraped one.

    Replacement, not merge: a member who lost the role must disappear, and a stale row
    would keep them in every future count.
    """
    await session.execute(delete(RosterMember).where(RosterMember.ladder_id == ladder_id))
    for member in members:
        session.add(
            RosterMember(
                ladder_id=ladder_id,
                user_id=member["user_id"],
                display_name=member.get("display_name", ""),
                tier_key=member.get("tier_key"),
                tier_label=member.get("tier_label"),
                tier_ordinal=member.get("tier_ordinal"),
                required_per_week=member.get("required_per_week", 0),
            )
        )
    await session.flush()
    return len(members)


async def list_roster(session: AsyncSession, ladder_id: int) -> Sequence[RosterMember]:
    return (
        await session.scalars(
            select(RosterMember)
            .where(RosterMember.ladder_id == ladder_id)
            .order_by(RosterMember.tier_ordinal.desc().nulls_last())
        )
    ).all()


async def get_run_by_period(
    session: AsyncSession, *, binding_id: int, period_start: datetime, period_end: datetime
) -> WeeklyRun | None:
    return await session.scalar(
        select(WeeklyRun).where(
            WeeklyRun.binding_id == binding_id,
            WeeklyRun.period_start == period_start,
            WeeklyRun.period_end == period_end,
        )
    )


async def put_run(
    session: AsyncSession,
    *,
    binding_id: int,
    period_start: datetime,
    period_end: datetime,
    status: str,
    summary: dict[str, Any],
) -> WeeklyRun:
    run = await get_run_by_period(
        session, binding_id=binding_id, period_start=period_start, period_end=period_end
    )
    if run is None:
        run = WeeklyRun(
            binding_id=binding_id,
            period_start=period_start,
            period_end=period_end,
            status=status,
            summary=summary,
        )
        session.add(run)
        await session.flush()
        return run
    run.status = status
    run.summary = summary
    return run


async def replace_run_results(
    session: AsyncSession, run_id: int, results: Sequence[dict[str, Any]]
) -> None:
    """Rewrite a run's results. Re-running a period must not duplicate people."""
    await session.execute(delete(WeeklyResult).where(WeeklyResult.run_id == run_id))
    for row in results:
        session.add(WeeklyResult(run_id=run_id, **row))
    await session.flush()


async def list_run_results(session: AsyncSession, run_id: int) -> Sequence[WeeklyResult]:
    return (
        await session.scalars(
            select(WeeklyResult)
            .where(WeeklyResult.run_id == run_id)
            .order_by(WeeklyResult.approved_count.desc())
        )
    ).all()


async def set_run_report_message(
    session: AsyncSession,
    run_id: int,
    *,
    channel_id: int,
    message_ids: Sequence[int],
) -> None:
    """Remember where a published report lives, so a re-run edits instead of duplicating."""
    run = await session.get(WeeklyRun, run_id)
    if run is None:
        return
    run.report_channel_id = channel_id
    run.report_message_id = message_ids[0] if message_ids else None
    run.published_ids = list(message_ids)


async def list_runs_for_binding(
    session: AsyncSession, binding_id: int, *, limit: int = 12
) -> Sequence[WeeklyRun]:
    return (
        await session.scalars(
            select(WeeklyRun)
            .where(WeeklyRun.binding_id == binding_id)
            .order_by(WeeklyRun.period_start.desc())
            .limit(limit)
        )
    ).all()


async def get_binding_by_id(session: AsyncSession, binding_id: int) -> ChannelBinding | None:
    return await session.get(ChannelBinding, binding_id)


async def get_quota_policies(
    session: AsyncSession, *, ladder_id: int, binding_id: int
) -> Sequence[QuotaPolicy]:
    """Requirements that apply to this binding, most specific first.

    A policy with a `binding_id` overrides one with none, so an admin can set a default
    for the whole ladder and then adjust one channel.
    """
    return (
        await session.scalars(
            select(QuotaPolicy)
            .where(
                QuotaPolicy.ladder_id == ladder_id,
                (QuotaPolicy.binding_id == binding_id) | (QuotaPolicy.binding_id.is_(None)),
            )
            .order_by(QuotaPolicy.binding_id.is_(None))
        )
    ).all()


async def get_tier_ordinals(session: AsyncSession, ladder_id: int) -> dict[int, int]:
    """tier id -> ordinal, for joining quota policies onto a roster snapshot."""
    rows = (
        await session.execute(
            select(RankTier.id, RankTier.ordinal).where(RankTier.ladder_id == ladder_id)
        )
    ).all()
    return {int(row[0]): int(row[1]) for row in rows}


# --- reaction cache --------------------------------------------------------
#
# "Who reacted" costs one request per emoji per message and is rate limited per channel,
# which is what makes a scan of an old period slow. Discord's own message payload carries
# the reaction *counts* on every read, so if a message's counts are unchanged the same
# people are still the ones who reacted and the reactor list does not need fetching again.
#
# The cache is keyed on the counts, not only the message id, so a new approval invalidates
# the entry automatically. Stale data is therefore impossible rather than merely unlikely.


@dataclass(frozen=True, slots=True)
class CachedReaction:
    user_id: int
    emoji_name: str | None


async def get_reaction_counts(session: AsyncSession, message_id: int) -> dict[str, int]:
    """Stored counts per emoji, used to decide whether a cache entry is still valid."""
    rows = (
        await session.execute(
            select(MessageReaction.emoji_key, func.count(MessageReaction.id))
            .where(MessageReaction.message_id == message_id)
            .group_by(MessageReaction.emoji_key)
        )
    ).all()
    return {str(key): int(count) for key, count in rows}


async def get_reaction_cache_time(session: AsyncSession, message_id: int) -> datetime | None:
    """When the reactor list for this message was last stored by a scan.

    Used to decide whether the cache can describe what is on the message right now: an
    entry written before the message was last edited cannot account for a change.
    """
    return await session.scalar(
        select(func.max(MessageReaction.added_at)).where(MessageReaction.message_id == message_id)
    )


async def list_cached_reactions(session: AsyncSession, message_id: int) -> list[CachedReaction]:
    rows = (
        await session.scalars(
            select(MessageReaction)
            .where(MessageReaction.message_id == message_id)
            .order_by(MessageReaction.added_at)
        )
    ).all()
    return [CachedReaction(user_id=row.user_id, emoji_name=row.emoji_name) for row in rows]


async def replace_cached_reactions(
    session: AsyncSession,
    *,
    message_id: int,
    channel_id: int,
    guild_id: int | None,
    reactions: Sequence[tuple[int, str | None, int | None, str]],
) -> None:
    """Store the reactor list for one message, replacing what was there.

    Written with `cache` as the source so a later scan can tell these came from a report
    read rather than from a live reaction event.
    """
    await session.execute(delete(MessageReaction).where(MessageReaction.message_id == message_id))
    for user_id, emoji_name, emoji_id, emoji_key in reactions:
        session.add(
            MessageReaction(
                message_id=message_id,
                channel_id=channel_id,
                guild_id=guild_id,
                emoji_key=emoji_key,
                emoji_id=emoji_id,
                emoji_name=emoji_name,
                user_id=user_id,
            )
        )
    await session.flush()

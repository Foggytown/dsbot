"""The reports module: message group -> parsed report -> validation reply.

This is the piece that actually does the job in the channel. It owns:
  * assembling a message group (a report plus its follow-up photos)
  * parsing and persisting the result
  * posting, maintaining, and clearing the warning message

It knows nothing about verdicts or quotas. Those subscribe to the same events later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from dsbot.modules.reports.parser import (
    BoundField,
    GroupMessage,
    ParseOutput,
    ReportCandidate,
    parse_report,
)
from dsbot.modules.reports.schema import FormSchemaDef, from_definition
from dsbot.modules.reports.templates import (
    DEFAULT_LOCALE,
    REPORT_ISSUES,
    SUPPORTED_LOCALES,
    issue_line,
)
from dsbot.storage import repos
from dsbot.storage.db import Database

if TYPE_CHECKING:
    from dsbot.discord.reply import ReplySink


class RetryScheduler(Protocol):
    """Schedules a delayed re-check of one report.

    Declared as a protocol so the reports module does not depend on the scheduler
    implementation, and tests can use a purely in-memory one.
    """

    async def schedule(
        self, *, when: datetime, channel_id: int, message_id: int, key: str
    ) -> None: ...


log = structlog.get_logger(__name__)

# How long a report may wait for follow-up attachments before being warned about.
DEFAULT_GROUP_WINDOW = timedelta(minutes=2)


@dataclass(slots=True)
class ChannelContext:
    """Everything the reports module needs about one channel."""

    binding_id: int
    channel_id: int
    guild_id: int | None
    locale: str
    schemas: list[FormSchemaDef]
    group_window: timedelta


@dataclass(slots=True)
class ProcessResult:
    report_id: int
    matched: bool
    valid: bool
    issues: list[str]
    field_count: int
    action: str  # warned | cleared | none | stored


class ReportService:
    # How far back to look for a report still waiting for this author's attachments.
    pending_lookback: timedelta = timedelta(minutes=30)

    def __init__(
        self,
        db: Database,
        reply: ReplySink | None = None,
        retry: RetryScheduler | None = None,
        *,
        group_window: timedelta | None = None,
    ) -> None:
        self._db = db
        self._reply = reply
        self._retry = retry
        # Operator-level override; per-channel config can still differ.
        self._group_window = group_window

    def attach_retry(self, retry: RetryScheduler) -> None:
        """Late binding, because the scheduler needs the service to exist first."""
        self._retry = retry

    # --- context -----------------------------------------------------------

    async def channel_context(self, channel_id: int) -> ChannelContext | None:
        async with self._db.session() as session:
            binding = await repos.get_binding_by_channel(session, channel_id)
            if binding is None or binding.module_key != "reports":
                return None
            rows = await repos.get_schemas_for_binding(session, binding.id)
            schemas = [from_definition(row.definition) for row in rows]
            config = binding.config or {}
            window = self._group_window
            if window is None:
                window = timedelta(minutes=float(config.get("group_window_minutes", 2)))
            return ChannelContext(
                binding_id=binding.id,
                channel_id=channel_id,
                guild_id=binding.guild_id,
                locale=_locale(binding.locale),
                schemas=schemas,
                group_window=window,
            )

    # --- processing --------------------------------------------------------

    async def process_message(
        self, channel_id: int, author_id: int, message_id: int
    ) -> ProcessResult | None:
        """Parse the group containing `message_id` and reconcile the warning."""
        context = await self.channel_context(channel_id)
        if context is None or not context.schemas:
            return None

        group = await self._load_group(context, author_id, message_id)
        if group is None:
            return None

        return await self.process_group(context, group)

    async def process_group(
        self, context: ChannelContext, group: list[GroupMessage]
    ) -> ProcessResult | None:
        """Parse an assembled group. Shared by live ingest, edits, retries and replay."""
        return await self._evaluate(context, group, phase="initial")

    async def _evaluate(
        self, context: ChannelContext, group: list[GroupMessage], *, phase: str
    ) -> ProcessResult | None:
        """One parsing pass. `phase` decides whether the grace window still applies."""
        if not group:
            return None
        candidate = ReportCandidate(messages=group)
        parsed = parse_report(context.schemas, candidate)

        issue_codes = [i.code for i in parsed.issues]
        if not parsed.matched:
            report_id = await self._persist(context, candidate, parsed, status="ignored")
            # Not a report attempt at all. Do not nag: an unbound-looking message in a
            # report channel is chat, not an error.
            return ProcessResult(
                report_id=report_id,
                matched=False,
                valid=False,
                issues=issue_codes,
                field_count=0,
                action="stored",
            )

        if parsed.is_valid:
            report_id = await self._persist(context, candidate, parsed, status="valid")
            cleared = await self._clear_warning(context, report_id)
            return ProcessResult(
                report_id=report_id,
                matched=True,
                valid=True,
                issues=[],
                field_count=len(parsed.fields),
                action="cleared" if cleared else "none",
            )

        # Still inside the grace window: wait for the follow-up photos.
        if phase != "retry" and self._awaiting_attachments(parsed, group, context):
            report_id = await self._persist(context, candidate, parsed, status="deferred")
            await self._schedule_retry(context, group, report_id)
            return ProcessResult(
                report_id=report_id,
                matched=True,
                valid=False,
                issues=issue_codes,
                field_count=len(parsed.fields),
                action="deferred",
            )

        report_id = await self._persist(context, candidate, parsed, status="invalid")
        await self._warn(context, report_id, parsed, author_id=group[0].author_id)
        return ProcessResult(
            report_id=report_id,
            matched=True,
            valid=False,
            issues=issue_codes,
            field_count=len(parsed.fields),
            action="warned",
        )

    async def retry_group(self, channel_id: int, message_id: int) -> ProcessResult | None:
        """Re-examine a deferred report after its grace window expired."""
        context = await self.channel_context(channel_id)
        if context is None or not context.schemas:
            return None
        async with self._db.session() as session:
            message = await repos.get_message(session, channel_id, message_id)
            if message is None:
                return None
            author_id = message.author_id

        group = await self._load_group(context, author_id, message_id)
        if group is None:
            return None
        return await self._evaluate(context, group, phase="retry")

    async def _load_group(
        self, context: ChannelContext, author_id: int, message_id: int
    ) -> list[GroupMessage] | None:
        """Load the report message plus later messages by the same author.

        Grouping rule: same author, same channel, inside the group window, and only
        messages that plausibly continue a report (attachment-only, or a new report
        start is excluded because it would begin a group of its own).
        """
        async with self._db.session() as session:
            message = await repos.get_message(session, context.channel_id, message_id)
            if message is None or message.deleted_at is not None:
                return None
            if message.author_id != author_id:
                return None
            if message.author_is_bot:
                return None

            cutoff = message.created_at + context.group_window
            later = await repos.list_messages_between(
                session,
                context.channel_id,
                message.created_at,
                cutoff,
                author_id=author_id,
            )

        group: list[GroupMessage] = [
            GroupMessage(
                message_id=message.message_id,
                author_id=message.author_id,
                created_at=_aware(message.created_at),
                content=message.content,
                attachment_count=len(message.attachments or []),
            )
        ]

        for follower in later:
            if follower.message_id == message.message_id:
                continue
            has_attachments = bool(follower.attachments)
            is_empty = not (follower.content or "").strip()
            # Only attachment-carrying or empty follow-ups continue a report. A new
            # numbered message is a separate report.
            if has_attachments or is_empty:
                group.append(
                    GroupMessage(
                        message_id=follower.message_id,
                        author_id=follower.author_id,
                        created_at=_aware(follower.created_at),
                        content=follower.content,
                        attachment_count=len(follower.attachments or []),
                    )
                )
            else:
                break

        return group

    def _awaiting_attachments(
        self,
        parsed: ParseOutput,
        group: list[GroupMessage],
        context: ChannelContext,
    ) -> bool:
        """True while missing evidence could still arrive in a follow-up."""
        missing_evidence = [
            i for i in parsed.issues if i.code == "field_missing" and i.field_id is not None
        ]
        if not missing_evidence:
            return False

        specs = {b.spec.id: b.spec for b in parsed.fields}
        needs_attachments = any(
            specs[field_id].accepts_attachments
            for field_id in (i.field_id for i in missing_evidence)
            if field_id in specs
        )
        if not needs_attachments:
            return False

        # The deadline runs from the *report* message, not the newest follower, so a
        # stream of chatter cannot postpone the warning indefinitely.
        first_message_at = min(m.created_at for m in group)
        return datetime.now(UTC) - first_message_at < context.group_window

    async def _schedule_retry(
        self,
        context: ChannelContext,
        group: list[GroupMessage],
        report_id: int,
    ) -> None:
        """Fire a re-check when the grace window expires.

        Without this, a deferred report is a dead end: nothing else ever looks at it
        again if the author simply stops posting.
        """
        if self._retry is None:
            return
        first_message_at = min(m.created_at for m in group)
        due = first_message_at + context.group_window + timedelta(seconds=2)
        await self._retry.schedule(
            when=due,
            channel_id=context.channel_id,
            message_id=group[0].message_id,
            key=f"{context.channel_id}:{group[0].message_id}",
        )

    async def pending_for_author(
        self, channel_id: int, author_id: int, after_message_id: int
    ) -> list[int]:
        """Find reports by this author still awaiting evidence.

        This is the fix for "photo posted, report never re-checked": a follow-up
        message cannot know which earlier report it belongs to, so the reports that
        are still waiting for it have to be found and re-examined.

        The database already knows which ones are waiting, so ask it rather than
        guessing from message text. Chat is stored as `ignored` and cannot match.
        """
        async with self._db.session() as session:
            message = await repos.get_message(session, channel_id, after_message_id)
            if message is None:
                return []
            rows = await repos.list_pending_reports(
                session,
                channel_id=channel_id,
                author_id=author_id,
                after=message.created_at - self.pending_lookback,
                before=message.created_at,
            )
        return [row.message_id for row in rows]

    # --- persistence -------------------------------------------------------

    async def _persist(
        self,
        context: ChannelContext,
        candidate: ReportCandidate,
        parsed: ParseOutput,
        *,
        status: str,
    ) -> int:
        primary = candidate.primary
        field_rows = [
            {
                "field_id": bound.spec.id,
                "path": ".".join(str(p) for p in bound.item_path) if bound.item_path else None,
                "label": bound.label_text,
                "raw_value": bound.raw_value or None,
                "value": _json_safe(bound.result.value),
                "present": bound.result.present,
                "resolution": bound.resolution,
            }
            for bound in parsed.fields
        ]

        async with self._db.write_session() as session:
            report = await repos.upsert_report(
                session,
                channel_id=context.channel_id,
                message_id=primary.message_id,
                guild_id=context.guild_id,
                author_user_id=primary.author_id,
                created_at=primary.created_at,
                binding_id=context.binding_id,
                schema_id=None,
                schema_key=parsed.schema_key,
            )
            report.status = status
            report.parsing = _json_safe(parsed.diagnostics)
            report.issues = [i.to_payload() for i in parsed.issues]
            report.attributed_user_id = primary.author_id
            report.attributed_field = parsed.attribution_field
            report.attributed_raw = parsed.attribution_raw
            report.attribution_method = "field" if parsed.attribution_field else "self"
            await repos.replace_report_fields(session, report.id, field_rows)
            await session.flush()
            return report.id

    # --- replies -----------------------------------------------------------

    async def _warn(
        self,
        context: ChannelContext,
        report_id: int,
        parsed: ParseOutput,
        author_id: int,
    ) -> None:
        if self._reply is None:
            return

        body = self._compose_warning(context.locale, parsed, author_id)
        existing = None
        async with self._db.session() as session:
            existing = await repos.get_validation_message(session, report_id)

        post_message_id = await self._post_or_edit(
            context, existing.message_id if existing else None, body
        )
        if post_message_id is None:
            return

        async with self._db.write_session() as session:
            await repos.put_validation_message(
                session,
                report_id=report_id,
                channel_id=context.channel_id,
                message_id=post_message_id,
                issue_codes=[i.code for i in parsed.issues],
            )

    async def _clear_warning(self, context: ChannelContext, report_id: int) -> bool:
        if self._reply is None:
            return False
        async with self._db.session() as session:
            existing = await repos.get_validation_message(session, report_id)
        if existing is None:
            return False

        cleared = await self._reply.clear(existing.channel_id, existing.message_id)
        async with self._db.write_session() as session:
            await repos.delete_validation_message(session, report_id)
        if cleared:
            log.info("warning_cleared", report_id=report_id)
        return cleared

    async def _post_or_edit(
        self, context: ChannelContext, message_id: int | None, body: str
    ) -> int | None:
        assert self._reply is not None
        if message_id is not None:
            edited = await self._reply.edit(context.channel_id, message_id, body)
            if edited:
                return message_id
            message_id = None
        return await self._reply.send(context.channel_id, body)

    def _compose_warning(self, locale: str, parsed: ParseOutput, author_id: int) -> str:
        template = REPORT_ISSUES.get(locale) or REPORT_ISSUES[DEFAULT_LOCALE]
        lines = [
            issue_line(
                issue.code,
                locale,
                field_hint=_field_hint(issue.field_id, parsed),
                **_detail_context(issue.detail),
            )
            for issue in parsed.issues
            if issue.severity == "error"
        ]
        return template.format(mention=f"<@{author_id}>", lines="\n".join(lines))


def _detail_context(detail: dict[str, Any] | None) -> dict[str, str]:
    """Expose issue detail as flat template variables like `detail_raw`."""
    if not detail:
        return {}
    return {f"detail_{key}": str(value) for key, value in detail.items()}


def _field_hint(field_id: str | None, parsed: ParseOutput) -> str:
    if field_id is None:
        return "?"
    for bound in parsed.fields:
        if bound.spec.id == field_id:
            if bound.spec.hint:
                return bound.spec.hint.get("ru") or next(iter(bound.spec.hint.values()))
            if bound.spec.labels:
                return bound.spec.labels[0]
    return field_id


def _status_for(parsed: ParseOutput) -> str:
    if not parsed.matched:
        return "ignored"
    if parsed.is_valid:
        return "valid"
    return "invalid"


def _locale(value: str | None) -> str:
    if value and value in SUPPORTED_LOCALES:
        return value
    return DEFAULT_LOCALE


def _aware(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _json_safe(value: Any) -> Any:
    """JSON columns must accept whatever the parser produced, including dataclasses."""
    import json

    if value is None:
        return None
    return json.loads(json.dumps(value, default=str))


__all__ = [
    "DEFAULT_GROUP_WINDOW",
    "BoundField",
    "ChannelContext",
    "ProcessResult",
    "ReportService",
]

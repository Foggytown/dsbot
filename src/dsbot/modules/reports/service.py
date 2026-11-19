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
        allow_bot_authors: bool = False,
    ) -> None:
        self._db = db
        self._reply = reply
        self._retry = retry
        # Operator-level override; per-channel config can still differ.
        self._group_window = group_window
        # Dev only: lets a test message authored by the bot itself flow through the
        # same pipeline, which is the only way to exercise the live gateway path
        # without a second Discord account.
        self._allow_bot_authors = allow_bot_authors
        # The message currently being evaluated, so a reply can quote it.
        self._report_message_id: int = 0

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
        self._report_message_id = group[0].message_id
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

        # No deferral. A warning that arrives two minutes late feels broken, and a
        # deferred report that dies with the process is worse. Warn immediately; if the
        # photos arrive later the warning is cleared automatically.
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
        """Re-examine one report on demand.

        Only used by `dsbot replay` and the startup sweep. Live traffic never defers, so
        nothing schedules this automatically.
        """
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
            if message.author_is_bot and not self._allow_bot_authors:
                log.debug(
                    "bot_authored_message_ignored",
                    message_id=message_id,
                )
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
            content = (follower.content or "").strip()
            # Only an attachment-only message continues a report. Anything with text is
            # either a new report or chat, and joining it causes real corruption: an
            # earlier report without photos once stole the photos of a later one because
            # that later message had both text and attachments.
            if content:
                break
            group.append(_group_message(follower))

        return group

    async def pending_for_author(
        self, channel_id: int, author_id: int, after_message_id: int
    ) -> list[int]:
        """Find reports by this author that a new attachment-only message may complete.

        A follow-up photo cannot know which earlier report it belongs to, so the reports
        that are still waiting for evidence must be found and re-examined. Without this a
        photo posted moments after its report never counted.
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
            log.warning("no_reply_sink_configured", report_id=report_id)
            return

        body = self._compose_warning(context.locale, parsed, author_id)
        async with self._db.session() as session:
            existing = await repos.get_validation_message(session, report_id)

        post_message_id = await self._post_or_edit(
            context,
            existing.message_id if existing else None,
            body,
            report_message_id=self._report_message_id,
        )
        if post_message_id is None:
            # Never silently swallow this: a report that should have been warned about
            # and was not is indistinguishable from the bot being broken.
            log.error(
                "warning_not_posted",
                report_id=report_id,
                channel_id=context.channel_id,
                issues=[i.code for i in parsed.issues],
            )
            return

        async with self._db.write_session() as session:
            await repos.put_validation_message(
                session,
                report_id=report_id,
                channel_id=context.channel_id,
                message_id=post_message_id,
                issue_codes=[i.code for i in parsed.issues],
            )
        log.info(
            "warning_posted",
            report_id=report_id,
            channel_id=context.channel_id,
            message_id=post_message_id,
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
        self,
        context: ChannelContext,
        message_id: int | None,
        body: str,
        report_message_id: int,
    ) -> int | None:
        assert self._reply is not None
        if message_id is not None:
            edited = await self._reply.edit(context.channel_id, message_id, body)
            if edited:
                return message_id
            message_id = None
        return await self._reply.send(
            context.channel_id, body, reference_message_id=report_message_id
        )

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


def _group_message(message: Any) -> GroupMessage:
    return GroupMessage(
        message_id=message.message_id,
        author_id=message.author_id,
        created_at=_aware(message.created_at),
        content=message.content,
        attachment_count=len(message.attachments or []),
    )


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

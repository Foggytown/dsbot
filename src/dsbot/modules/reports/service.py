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
from typing import TYPE_CHECKING, Any

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

log = structlog.get_logger(__name__)

# How long a report may wait for follow-up attachments before being warned about.
DEFAULT_GROUP_WINDOW = timedelta(minutes=5)


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
    def __init__(self, db: Database, reply: ReplySink | None = None) -> None:
        self._db = db
        self._reply = reply

    # --- context -----------------------------------------------------------

    async def channel_context(self, channel_id: int) -> ChannelContext | None:
        async with self._db.session() as session:
            binding = await repos.get_binding_by_channel(session, channel_id)
            if binding is None or binding.module_key != "reports":
                return None
            rows = await repos.get_schemas_for_binding(session, binding.id)
            schemas = [from_definition(row.definition) for row in rows]
            config = binding.config or {}
            window_minutes = int(config.get("group_window_minutes", 5))
            return ChannelContext(
                binding_id=binding.id,
                channel_id=channel_id,
                guild_id=binding.guild_id,
                locale=_locale(binding.locale),
                schemas=schemas,
                group_window=timedelta(minutes=window_minutes),
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

        candidate = ReportCandidate(messages=group)
        parsed = parse_report(context.schemas, candidate)
        report_id = await self._persist(context, candidate, parsed)

        issue_codes = [i.code for i in parsed.issues]
        if not parsed.matched:
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
            cleared = await self._clear_warning(context, report_id)
            return ProcessResult(
                report_id=report_id,
                matched=True,
                valid=True,
                issues=[],
                field_count=len(parsed.fields),
                action="cleared" if cleared else "none",
            )

        # Defer the warning if attachments may still be on their way.
        if self._awaiting_attachments(parsed, group, context):
            return ProcessResult(
                report_id=report_id,
                matched=True,
                valid=False,
                issues=issue_codes,
                field_count=len(parsed.fields),
                action="deferred",
            )

        await self._warn(context, report_id, parsed, author_id)
        return ProcessResult(
            report_id=report_id,
            matched=True,
            valid=False,
            issues=issue_codes,
            field_count=len(parsed.fields),
            action="warned",
        )

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
        self, parsed: ParseOutput, group: list[GroupMessage], context: ChannelContext
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

        last_message_at = max(m.created_at for m in group)
        return datetime.now(UTC) - last_message_at < context.group_window

    # --- persistence -------------------------------------------------------

    async def _persist(
        self, context: ChannelContext, candidate: ReportCandidate, parsed: ParseOutput
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
            report.status = _status_for(parsed)
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

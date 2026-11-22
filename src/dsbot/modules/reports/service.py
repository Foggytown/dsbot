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
DEFAULT_GROUP_WINDOW = timedelta(minutes=2)

# The default ceiling on warning about an old report.
#
# Deliberately a default, not an opt-in. Every caller - live bot, replay, backfill -
# re-evaluates messages it did not receive now, and a forgotten argument would restore
# the bug where the bot complained about messages from half a year ago. Live reports
# arrive within seconds, so a day is far more generous than the real case.
DEFAULT_WARN_MAX_AGE = timedelta(hours=24)


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
        *,
        group_window: timedelta | None = None,
        allow_bot_authors: bool = False,
        contact_user_id: int | None = None,
        warn_max_age: timedelta | None = DEFAULT_WARN_MAX_AGE,
    ) -> None:
        self._db = db
        self._reply = reply
        # Operator-level override; per-channel config can still differ.
        self._group_window = group_window
        # Dev only: lets a test message authored by the bot itself flow through the
        # same pipeline, which is the only way to exercise the live gateway path
        # without a second Discord account.
        self._allow_bot_authors = allow_bot_authors
        # Who to ping when a reporter needs a human. Shown in every warning.
        self.contact_user_id = contact_user_id
        # Never nag the channel about a report from long ago. A reaction, an edit or a
        # reconciliation pass can re-evaluate an old message at any time, and none of
        # them mean "this report is late". None disables the check.
        self._warn_max_age = warn_max_age

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
        self,
        context: ChannelContext,
        group: list[GroupMessage],
        *,
        phase: str,
    ) -> ProcessResult | None:
        """One parsing pass. `phase` decides whether the grace window still applies."""
        if not group:
            return None
        head = group[0]
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

        # An existing warning must always be maintained, however old the report is: the
        # age limit is about not CREATING a new complaint about history, never about
        # leaving a live report's warning frozen after the author fixed it.
        async with self._db.session() as session:
            existing = await repos.get_validation_message(session, report_id)

        if not self._may_warn(
            head,
            channel_id=context.channel_id,
            report_id=report_id,
            issues=issue_codes,
            has_warning=existing is not None,
        ):
            return ProcessResult(
                report_id=report_id,
                matched=True,
                valid=False,
                issues=issue_codes,
                field_count=len(parsed.fields),
                action="stored",
            )
        await self._warn(
            context,
            report_id,
            parsed,
            author_id=head.author_id,
            report_message_id=head.message_id,
        )
        return ProcessResult(
            report_id=report_id,
            matched=True,
            valid=False,
            issues=issue_codes,
            field_count=len(parsed.fields),
            action="warned",
        )

    def _may_warn(
        self,
        head: GroupMessage,
        *,
        channel_id: int,
        report_id: int,
        issues: list[str],
        has_warning: bool = False,
    ) -> bool:
        """Is this report recent enough to complain about?

        The reply is a nudge to fix something that is still actionable. Re-evaluating a
        message from months ago - because somebody added a reaction, edited it, or a
        reconciliation sweep touched it - does not make it late, and warning then is
        indistinguishable from the bot malfunctioning.

        **A warning that already exists is always maintained**, however old the report is.
        Template checking is a live-only job; what the age limit prevents is the bot
        *starting* to nag about history. Letting it also block updates left warnings
        frozen: an author who fixed their report kept seeing the old complaint forever.
        """
        if has_warning:
            return True
        if self._warn_max_age is None or self._warn_max_age <= timedelta(0):
            return True
        created = head.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = datetime.now(UTC) - created
        if age <= self._warn_max_age:
            return True
        log.info(
            "warning_suppressed_too_old",
            channel_id=channel_id,
            report_id=report_id,
            report_message_id=head.message_id,
            age_hours=round(age.total_seconds() / 3600, 1),
            max_age_hours=round(self._warn_max_age.total_seconds() / 3600, 1),
            issues=issues,
        )
        return False

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
                author_name=message.author_display_name or message.author_name,
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
        *,
        report_message_id: int,
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
            report_message_id=report_message_id,
        )
        if post_message_id is None:
            # Never silently swallow this: a report that should have been warned about
            # and was not is indistinguishable from the bot being broken.
            log.error(
                "warning_not_posted",
                channel_id=context.channel_id,
                report_id=report_id,
                report_message_id=report_message_id,
                issues=[i.code for i in parsed.issues],
            )
            return

        async with self._db.write_session() as session:
            await repos.put_validation_message(
                session,
                report_id=report_id,
                channel_id=context.channel_id,
                message_id=post_message_id,
                report_message_id=report_message_id,
                issue_codes=[i.code for i in parsed.issues],
            )
        # Log both ids. `message_id` is the warning the bot posted; on its own it says
        # nothing about *which* report was complained about, which made a real incident
        # impossible to diagnose from the log: every line was a bot message id, and the
        # only way back to the report was a database query.
        log.info(
            "warning_posted",
            channel_id=context.channel_id,
            report_id=report_id,
            report_message_id=report_message_id,
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

    def _compose_warning(
        self,
        locale: str,
        parsed: ParseOutput,
        author_id: int,
    ) -> str:
        template = REPORT_ISSUES.get(locale) or REPORT_ISSUES[DEFAULT_LOCALE]
        lines: list[str] = []
        for issue in parsed.issues:
            if issue.severity not in ("error", "info"):
                continue
            # Informational entries exist to print the allowed lists.
            if issue.severity == "info" and not issue.code.startswith("list_"):
                continue
            lines.append(
                issue_line(
                    issue.code,
                    locale,
                    field_hint=_field_hint(issue.field_id, parsed),
                    **_detail_context(issue.detail),
                )
            )

        return template.format(
            mention=f"<@{author_id}>",
            contact=f"<@{self.contact_user_id}>" if self.contact_user_id else "-",
            lines="\n".join(lines),
        )


def _group_message(message: Any) -> GroupMessage:
    return GroupMessage(
        message_id=message.message_id,
        author_id=message.author_id,
        created_at=_aware(message.created_at),
        content=message.content,
        attachment_count=len(message.attachments or []),
        author_name=message.author_display_name or message.author_name,
    )


def _detail_context(detail: dict[str, Any] | None) -> dict[str, str]:
    """Expose issue detail as flat template variables like `detail_raw`.

    Lists are joined with `, ` so a warning can print the allowed formations or
    qualifications on one readable line.
    """
    if not detail:
        return {}
    context: dict[str, str] = {}
    for key, value in detail.items():
        if isinstance(value, list):
            context[f"detail_{key}"] = ", ".join(str(v) for v in value)
        else:
            context[f"detail_{key}"] = str(value)
    return context


def _field_hint(field_id: str | None, parsed: ParseOutput) -> str:
    """A short name for a field, for use in a warning line.

    A hint sentence is too long to read inside «...», so the label is preferred and the
    hint is only a fallback. Without a label, the form's numbered path is used - and when
    the path is a sub-item like `3.2`, the parent number is used, because a reporter
    thinks in items ("пункт 3"), not in parser ids. Falling back to the raw field id
    produced "В поле «evidence_2»", which means nothing to anybody reading the channel.
    """
    if field_id is None:
        return "?"
    for bound in parsed.fields:
        if bound.spec.id != field_id:
            continue
        if bound.spec.labels:
            return bound.spec.labels[0]
        if bound.spec.path:
            return f"пункт {bound.spec.path.split('.')[0]}"
        # An evidence slot with no label of its own belongs to its parent item.
        if bound.item_path:
            return f"пункт {bound.item_path[0]}"
    return field_id


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

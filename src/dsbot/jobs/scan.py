"""Scan channel history and report the parser's verdict for every message.

Purpose: let an operator check the form against real traffic without waiting for the
live pipeline. This is deliberately read-only:

  * no reactions are fetched or considered
  * nothing is written to the database
  * nothing is posted to Discord
  * no warnings are generated

So it answers one question only: "would the parser accept this message, and what did
it extract?" That is exactly what you need when verifying a form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TextIO

from dsbot.modules.reports.parser import (
    GroupMessage,
    ReportCandidate,
    parse_report,
)
from dsbot.modules.reports.schema import FormSchemaDef
from dsbot.modules.reports.templates import issue_line

KIND_REPORT = "report"
KIND_PHOTO = "photo"
KIND_CHAT = "chat"


@dataclass(slots=True)
class ScanMessage:
    """One message as needed for scanning. No Discord objects, no reactions."""

    message_id: int
    author_id: int
    author_name: str
    content: str
    created_at: datetime
    attachment_count: int = 0
    attachment_names: tuple[str, ...] = ()


@dataclass(slots=True)
class ScanRow:
    message_id: int
    created_at: datetime
    author_name: str
    kind: str
    matched: bool
    valid: bool
    preview: str
    fields: dict[str, str] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    attachment_count: int = 0


@dataclass(slots=True)
class ScanReport:
    rows: list[ScanRow] = field(default_factory=list)
    schema_key: str | None = None
    total: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "total": self.total,
            "valid": sum(1 for r in self.rows if r.valid),
            "invalid": sum(1 for r in self.rows if r.matched and not r.valid),
            "not_a_report": sum(1 for r in self.rows if not r.matched and r.kind != KIND_PHOTO),
            "photo_followups": sum(1 for r in self.rows if r.kind == KIND_PHOTO),
        }


def _preview(content: str, limit: int = 60) -> str:
    flat = " / ".join(line.strip() for line in content.splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _field_summary(value: dict[str, Any] | None) -> str:
    """A compact, human-readable rendering of one parsed field."""
    if not isinstance(value, dict):
        return ""
    if "formation" in value:
        formation = value.get("formation") or "?"
        lore = value.get("lore_idn") or ""
        nrp = value.get("nrp_idn")
        prefix = value.get("prefix")
        idn = f"{prefix}/{lore}" if prefix and lore else lore
        text = f"{formation} | {idn}"
        if nrp:
            text += f" | {nrp}"
        return text
    if "code" in value:
        return str(value.get("code"))
    if "url" in value:
        return "link ok" if value.get("message_id") else "link"
    if "attachment_count" in value:
        return f"{value.get('attachment_count')} photo(s)"
    if "got" in value and "total" in value:
        return f"{value.get('got')}/{value.get('total')}"
    raw = value.get("raw")
    return str(raw) if raw else ""


def group_for_scan(
    messages: list[ScanMessage], *, max_gap_minutes: float = 5.0
) -> list[list[ScanMessage]]:
    """Group a report with its attachment-only follow-ups, mirroring live behaviour.

    A follower joins only when it has no text AND arrives within the gap window. Both
    conditions matter: text means a new message in its own right, and an unbounded
    window would attach a photo posted hours later to an unrelated report.
    """
    groups: list[list[ScanMessage]] = []
    for message in messages:
        is_empty_text = not message.content.strip()
        within_gap = False
        if groups:
            previous = groups[-1][-1]
            gap = (message.created_at - previous.created_at).total_seconds() / 60
            within_gap = gap <= max_gap_minutes
        if groups and is_empty_text and message.attachment_count > 0 and within_gap:
            groups[-1].append(message)
            continue
        groups.append([message])
    return groups


def scan_messages(
    messages: list[ScanMessage],
    schemas: list[FormSchemaDef],
    *,
    locale: str = "ru",
    max_gap_minutes: float = 5.0,
) -> ScanReport:
    """Evaluate every message, one row per report group."""
    report = ScanReport(total=len(messages))
    ordered = sorted(messages, key=lambda m: m.created_at)
    if schemas:
        report.schema_key = schemas[0].key

    for group in group_for_scan(ordered, max_gap_minutes=max_gap_minutes):
        head = group[0]
        attachments = sum(m.attachment_count for m in group)

        # A standalone attachment-only message is a follow-up that matched no report.
        if not head.content.strip() and attachments:
            report.rows.append(
                ScanRow(
                    message_id=head.message_id,
                    created_at=head.created_at,
                    author_name=head.author_name,
                    kind=KIND_PHOTO,
                    matched=False,
                    valid=False,
                    preview="(photo only)",
                    attachment_count=attachments,
                )
            )
            continue

        candidate = ReportCandidate(
            messages=[
                GroupMessage(
                    message_id=m.message_id,
                    author_id=m.author_id,
                    created_at=m.created_at,
                    content=m.content,
                    attachment_count=m.attachment_count,
                )
                for m in group
            ]
        )
        parsed = parse_report(schemas, candidate, attachment_count=attachments)

        kind = KIND_REPORT if parsed.matched else KIND_CHAT

        row = ScanRow(
            message_id=head.message_id,
            created_at=head.created_at,
            author_name=head.author_name,
            kind=kind,
            matched=parsed.matched,
            valid=parsed.is_valid,
            preview=_preview(head.content),
            attachment_count=attachments,
            issues=[i.to_payload() for i in parsed.issues],
        )
        if parsed.matched:
            for bound in parsed.fields:
                summary = _field_summary(bound.result.value)
                if summary:
                    row.fields[bound.spec.id] = summary
        report.rows.append(row)

    return report


def render(report: ScanReport, out: TextIO, *, locale: str = "ru") -> None:
    """Write a skimmable per-message verdict list."""
    counts = report.summary()
    out.write(f"schema: {report.schema_key or '(none)'}\n")
    out.write(
        "scanned {total} messages: {valid} valid, {invalid} invalid, "
        "{not_a_report} not a report, {photo_followups} photo follow-ups\n".format(**counts)
    )
    out.write("\n")

    for index, row in enumerate(report.rows, start=1):
        stamp = row.created_at.astimezone(UTC).strftime("%m-%d %H:%M")
        if row.kind == KIND_PHOTO:
            marker = "photo"
        elif row.valid:
            marker = "VALID"
        elif row.matched:
            marker = "INVALID"
        else:
            marker = "not a report"
        out.write(f"{index:>3}. [{marker:<12}] {stamp}  {row.author_name}\n")
        out.write(f"       text: {row.preview or '(empty)'}\n")
        if row.valid and row.fields:
            joined = "  |  ".join(f"{k}={v}" for k, v in row.fields.items())
            out.write(f"       parsed: {joined}\n")
        if row.matched and not row.valid:
            if row.fields:
                joined = "  |  ".join(f"{k}={v}" for k, v in row.fields.items())
                out.write(f"       parsed: {joined}\n")
            for issue in row.issues:
                if issue.get("severity") != "error":
                    continue
                detail = {f"detail_{k}": str(v) for k, v in (issue.get("detail") or {}).items()}
                line = issue_line(
                    str(issue.get("code")),
                    locale,
                    field_hint=str(issue.get("field") or "?"),
                    **detail,
                )
                out.write(f"       {line}\n")

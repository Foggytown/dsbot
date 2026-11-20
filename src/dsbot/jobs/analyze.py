"""Offline analysis of a backfill dump.

Purpose: measure how well the parser handles real channel traffic *before* trusting
it with a live week. Reads a JSONL export, runs the same parser the live path uses,
and reports match rate plus an issue histogram.

This is the tool that turns "hundreds of weeks of real data" into evidence.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from dsbot.modules.reports.parser import (
    GroupMessage,
    ReportCandidate,
    parse_report,
)
from dsbot.modules.reports.schema import FormSchemaDef
from dsbot.modules.reports.yaml_form import load_default_schemas


@dataclass(slots=True)
class AnalysisReport:
    total_messages: int = 0
    author_groups: int = 0
    matched: int = 0
    valid: int = 0
    matched_but_invalid: int = 0
    unmatched: int = 0
    issue_counts: Counter[str] = field(default_factory=Counter)
    schema_counts: Counter[str] = field(default_factory=Counter)
    unmatched_samples: list[str] = field(default_factory=list)
    invalid_samples: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "total_messages": self.total_messages,
            "author_groups": self.author_groups,
            "matched": self.matched,
            "valid": self.valid,
            "matched_but_invalid": self.matched_but_invalid,
            "unmatched": self.unmatched,
            "match_rate": round(self.matched / self.author_groups, 4)
            if self.author_groups
            else 0.0,
            "valid_rate_of_matched": round(self.valid / self.matched, 4) if self.matched else 0.0,
            "schema_counts": dict(self.schema_counts),
            "issue_counts": dict(self.issue_counts.most_common()),
        }


def read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def group_by_author(
    records: list[dict[str, Any]], *, window_minutes: int = 30
) -> list[list[dict[str, Any]]]:
    """Group messages the way the live service does.

    Same author, inside the window, and a follower only continues a group when it
    carries attachments or is empty. Any text starts a new group: absorbing it would
    splice unrelated messages into one report, which is a bug that actually occurred.
    """
    ordered = sorted(records, key=lambda r: r.get("created_at") or "")
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_author: int | None = None
    last_time: datetime | None = None

    for record in ordered:
        author = int(record.get("author_id") or 0)
        created = _parse_dt(record.get("created_at"))
        same_author = author == last_author
        close_in_time = (
            last_time is not None
            and created is not None
            and abs((created - last_time).total_seconds()) <= window_minutes * 60
        )
        has_attachments = bool(record.get("attachments"))
        is_empty = not str(record.get("content") or "").strip()
        continues = has_attachments or is_empty

        if current and same_author and close_in_time and continues:
            current.append(record)
        else:
            if current:
                groups.append(current)
            current = [record]
        last_author = author
        last_time = created

    if current:
        groups.append(current)
    return groups


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def analyze_records(
    records: list[dict[str, Any]],
    *,
    schemas: list[FormSchemaDef] | None = None,
    window_minutes: int = 30,
    sample_limit: int = 10,
) -> AnalysisReport:
    active = schemas if schemas is not None else load_default_schemas()
    report = AnalysisReport(total_messages=len(records))
    groups = group_by_author(records, window_minutes=window_minutes)
    report.author_groups = len(groups)

    for group in groups:
        messages = [
            GroupMessage(
                message_id=int(r.get("message_id") or 0),
                author_id=int(r.get("author_id") or 0),
                created_at=_parse_dt(r.get("created_at")) or datetime(1970, 1, 1, tzinfo=UTC),
                content=str(r.get("content") or ""),
                attachment_count=len(r.get("attachments") or []),
                urls=tuple(
                    a.get("url", "") for a in (r.get("attachments") or []) if isinstance(a, dict)
                ),
            )
            for r in group
        ]
        candidate = ReportCandidate(messages=messages)
        result = parse_report(active, candidate)

        if not result.matched:
            report.unmatched += 1
            if len(report.unmatched_samples) < sample_limit:
                report.unmatched_samples.append(messages[0].content[:400])
            continue

        report.matched += 1
        if result.schema_key:
            report.schema_counts[result.schema_key] += 1
        for issue in result.issues:
            report.issue_counts[issue.code] += 1

        if result.is_valid:
            report.valid += 1
        else:
            report.matched_but_invalid += 1
            if len(report.invalid_samples) < sample_limit:
                report.invalid_samples.append(
                    {
                        "content": messages[0].content[:400],
                        "issues": [i.to_payload() for i in result.issues],
                    }
                )

    return report


def write_report(report: AnalysisReport, out: TextIO) -> None:
    summary = report.summary()
    out.write(json.dumps(summary, ensure_ascii=False, indent=2))
    out.write("\n")

    if report.unmatched_samples:
        out.write("\n--- unmatched samples ---\n")
        for sample in report.unmatched_samples:
            out.write(f"\n{sample}\n")

    if report.invalid_samples:
        out.write("\n--- invalid samples ---\n")
        for sample in report.invalid_samples:
            out.write(f"\n{sample['content']}\n")
            out.write(f"  issues: {[i['code'] for i in sample['issues']]}\n")

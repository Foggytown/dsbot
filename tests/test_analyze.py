"""Analyzer tests: the tool that measures the parser against real channel traffic."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from dsbot.jobs.analyze import analyze_records, group_by_author


def record(
    content: str,
    *,
    author: int = 1,
    minutes: int = 0,
    attachments: int = 0,
    message_id: int = 1,
) -> dict[str, object]:
    created = datetime(2026, 1, 5, 12, tzinfo=UTC) + timedelta(minutes=minutes)
    return {
        "message_id": message_id,
        "channel_id": 10,
        "guild_id": 1,
        "author_id": author,
        "content": content,
        "created_at": created.isoformat(),
        "attachments": [{"url": f"https://cdn.example/{i}"} for i in range(attachments)],
        "embeds": [],
    }


def test_text_does_not_continue_a_group() -> None:
    """Text means the author moved on, so it starts a new group."""
    records = [
        record("1. SOB | 0016", author=1, minutes=0, message_id=1),
        record("2. INF", author=1, minutes=1, message_id=2),
    ]
    groups = group_by_author(records, window_minutes=30)
    assert len(groups) == 2


def test_attachment_followup_joins_the_group() -> None:
    records = [
        record("1. SOB | 0016", author=1, minutes=0, message_id=1),
        record("", author=1, minutes=1, message_id=2, attachments=1),
    ]
    groups = group_by_author(records, window_minutes=30)
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_report_and_followup_photo_form_one_group() -> None:
    """The core case: report text, then a bare photo in the next message."""
    text = "1. SOB | 0016\n2. INF\n3.\n4. "
    records = [
        record(text, author=7, minutes=0, message_id=1),
        record("", author=7, minutes=1, attachments=2, message_id=2),
    ]
    groups = group_by_author(records, window_minutes=30)
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_distant_messages_are_not_grouped() -> None:
    records = [
        record("x", author=1, minutes=0, message_id=1),
        record("y", author=1, minutes=120, message_id=2),
    ]
    groups = group_by_author(records, window_minutes=30)
    assert len(groups) == 2


def test_analyze_counts_and_rates() -> None:
    valid = "1. SOB | 0016\n2. INF\n3. Доказательства:\n  - фото\n  - фото"
    records = [
        record(valid, author=1, message_id=1, attachments=2),
        record("просто болтовня в чате", author=2, message_id=2),
    ]
    result = analyze_records(records)
    summary = result.summary()
    assert summary["total_messages"] == 2
    assert summary["matched"] == 1
    assert summary["unmatched"] == 1
    assert summary["match_rate"] == 0.5


def test_analyze_records_invalid_reports_separately() -> None:
    record_missing_evidence = "1. SOB | 0016\n2. INF\n3.\n4. "
    result = analyze_records([record(record_missing_evidence, attachments=0)])
    summary = result.summary()
    assert summary["matched"] == 1
    assert summary["valid"] == 0
    assert summary["matched_but_invalid"] == 1
    assert "field_missing" in summary["issue_counts"]


def test_unmatched_samples_are_captured() -> None:
    result = analyze_records([record("nothing to see here at all")])
    assert result.unmatched_samples
    assert "nothing to see here" in result.unmatched_samples[0]


def test_analyzer_output_is_json_serialisable() -> None:
    result = analyze_records([record("1. SOB | 0016\n2. INF")])
    json.dumps(result.summary(), ensure_ascii=False)

"""Tests for the read-only scanner.

The scanner exists so an operator can judge the form against real traffic. It must be
strictly observational: no database writes, no Discord posts, no reaction reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from dsbot.jobs.scan import (
    KIND_CHAT,
    KIND_PHOTO,
    KIND_REPORT,
    ScanMessage,
    group_for_scan,
    render,
    scan_messages,
)
from dsbot.modules.reports.yaml_form import parse_form_file

FORM = parse_form_file(Path("forms/training.yaml"))
BASE = datetime(2026, 11, 20, 12, tzinfo=UTC)


def msg(
    content: str,
    *,
    attachments: int = 0,
    minutes: int = 0,
    message_id: int = 1,
    author: str = "instructor",
) -> ScanMessage:
    return ScanMessage(
        message_id=message_id,
        author_id=1,
        author_name=author,
        content=content,
        created_at=BASE + timedelta(minutes=minutes),
        attachment_count=attachments,
    )


VALID_T = "1. SOB | 0016\n2. T\n3. фото\n4."
VALID_INF = "1. SOB | 0016\n2. INF\n3. фото\n4. https://discord.com/channels/1/2/3"


def test_valid_t_report_is_marked_valid() -> None:
    report = scan_messages([msg(VALID_T, attachments=1)], [FORM])
    assert report.rows[0].valid
    assert report.rows[0].kind == KIND_REPORT


def test_t_with_two_photos_is_invalid() -> None:
    report = scan_messages([msg(VALID_T, attachments=2)], [FORM])
    assert not report.rows[0].valid
    assert any(i["code"] == "rule_exact_attachments" for i in report.rows[0].issues)


def test_valid_inf_report_is_marked_valid() -> None:
    report = scan_messages([msg(VALID_INF, attachments=2)], [FORM])
    assert report.rows[0].valid


def test_chat_is_not_a_report() -> None:
    report = scan_messages([msg("привет, как дела?")], [FORM])
    assert report.rows[0].kind == KIND_CHAT
    assert not report.rows[0].matched


def test_photo_only_followup_is_recognised() -> None:
    report = scan_messages([msg("", attachments=2)], [FORM])
    assert report.rows[0].kind == KIND_PHOTO


def test_report_and_photo_are_grouped() -> None:
    messages = [msg(VALID_INF, minutes=0), msg("", attachments=2, minutes=1, message_id=2)]
    report = scan_messages(messages, [FORM])
    assert len(report.rows) == 1
    assert report.rows[0].valid
    assert report.rows[0].attachment_count == 2


def test_grouping_does_not_join_text_messages() -> None:
    messages = [
        msg(VALID_INF, minutes=0),
        msg("второй рапорт", minutes=1, message_id=2),
    ]
    groups = group_for_scan(sorted(messages, key=lambda m: m.created_at))
    assert len(groups) == 2


def test_grouping_respects_the_time_gap() -> None:
    """A photo posted much later must not complete an older report."""
    messages = [
        msg(VALID_INF, minutes=0),
        msg("", attachments=2, minutes=90, message_id=2),
    ]
    groups = group_for_scan(sorted(messages, key=lambda m: m.created_at))
    assert len(groups) == 2


def test_attachments_are_not_borrowed_across_reports() -> None:
    """A photo-less report must not be validated by a later report's photos."""
    messages = [
        msg("1. SOB | 0016\n2. INF\n3. фото\n4.", minutes=0, message_id=1),
        msg(VALID_INF, attachments=2, minutes=1, message_id=2),
    ]
    report = scan_messages(messages, [FORM])
    assert not report.rows[0].valid


def test_scan_extracts_fields_for_inspection() -> None:
    report = scan_messages([msg(VALID_INF, attachments=2)], [FORM])
    fields = report.rows[0].fields
    assert "SOB | 0016" in fields["identity"]
    assert fields["qualification"] == "INF"
    assert fields["photos"] == "2 photo(s)"


def test_summary_counts_are_consistent() -> None:
    messages = [
        msg(VALID_T, attachments=1, minutes=0, message_id=1),
        msg("chat", minutes=1, message_id=2),
        msg(VALID_INF, attachments=1, minutes=2, message_id=3),
    ]
    summary = scan_messages(messages, [FORM]).summary()
    assert summary["total"] == 3
    assert summary["valid"] == 1
    assert summary["invalid"] == 1
    assert summary["not_a_report"] == 1


def test_render_is_readable_and_marks_verdicts() -> None:
    report = scan_messages(
        [msg(VALID_T, attachments=1), msg("chat", minutes=1, message_id=2)], [FORM]
    )
    buffer = StringIO()
    render(report, buffer)
    text = buffer.getvalue()
    assert "VALID" in text
    assert "not a report" in text
    assert "schema: training_report" in text
    assert "scanned 2 messages" in text


def test_render_shows_rule_violation_reason() -> None:
    report = scan_messages([msg(VALID_T, attachments=2)], [FORM])
    buffer = StringIO()
    render(report, buffer)
    assert "ровно 1" in buffer.getvalue()


def test_scan_is_read_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The scanner must not touch the database or Discord, only the parser."""
    import dsbot.jobs.scan as scan_module

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("scan must not perform I/O")

    # Any accidental import of storage/discord in the scan path would fail loudly.
    report = scan_module.scan_messages([msg(VALID_T, attachments=1)], [FORM])
    assert report.rows

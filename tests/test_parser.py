"""End-to-end parser tests against real report text.

These are the tests that matter: they encode what staff actually accept.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS, training_report_schema
from tests.fixtures import reports

SCHEMAS = list(DEFAULT_SCHEMAS)


def make_group(content: str, *, attachments: int = 0) -> ReportCandidate:
    return ReportCandidate(
        messages=[
            GroupMessage(
                message_id=1,
                author_id=42,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                content=content,
                attachment_count=attachments,
            )
        ]
    )


def test_template_is_rejected_as_a_report() -> None:
    result = parse_report(SCHEMAS, make_group(reports.TEMPLATE))
    assert not result.matched
    assert any(i.code == "schema_unmatched" for i in result.issues)


def test_prose_is_rejected() -> None:
    result = parse_report(SCHEMAS, make_group(reports.GARBAGE))
    assert not result.matched


def test_real_lore_idn_report_matches() -> None:
    result = parse_report(SCHEMAS, make_group(reports.REAL_LORE_IDN))
    assert result.matched
    assert result.schema_key == "training_report"


def test_real_report_extracts_formation_and_idn() -> None:
    result = parse_report(SCHEMAS, make_group(reports.REAL_LORE_IDN))
    formation = result.field_value("formation_idn")
    assert formation is not None
    assert formation["formation"] == "SOB"
    assert formation["sub_unit"] == "01"
    assert formation["idn_number"] == "0016"


def test_real_report_extracts_qualification() -> None:
    result = parse_report(SCHEMAS, make_group(reports.REAL_LORE_IDN))
    qualification = result.field_value("qualification")
    assert qualification is not None
    assert qualification["code"] == "INF"


def test_real_report_extracts_promotion_link() -> None:
    result = parse_report(SCHEMAS, make_group(reports.REAL_LORE_IDN))
    link = result.field_value("promotion_link")
    assert link is not None
    assert link["message_id"] == 1570351536259731550


def test_real_report_is_invalid_without_photos() -> None:
    """The live example leaves field 3 empty; no follow-up arrived, so warn."""
    result = parse_report(SCHEMAS, make_group(reports.REAL_LORE_IDN), attachment_count=0)
    codes = {i.code for i in result.issues}
    assert "field_missing" in codes
    assert not result.is_valid


def test_real_report_becomes_valid_when_photos_arrive() -> None:
    """Same message, but the follow-up photos count. This is the core behaviour."""
    result = parse_report(SCHEMAS, make_group(reports.REAL_LORE_IDN), attachment_count=2)
    assert result.matched
    assert result.is_valid, [i.to_payload() for i in result.issues]


def test_inline_labels_are_matched_by_text_not_position() -> None:
    result = parse_report(SCHEMAS, make_group(reports.INLINE_LABELS), attachment_count=2)
    assert result.matched
    qualification = result.field_value("qualification")
    assert qualification is not None
    assert qualification["code"] == "MED"
    bindings = {b.spec.id: b for b in result.fields}
    assert bindings["qualification"].resolution == "label"


def test_missing_conditional_link_fails_inf_plus() -> None:
    result = parse_report(SCHEMAS, make_group(reports.MISSING_CONDITIONAL), attachment_count=2)
    missing = {i.field_id for i in result.issues if i.code == "field_missing"}
    assert "promotion_link" in missing
    assert not result.is_valid


def test_conditional_link_not_required_for_med() -> None:
    result = parse_report(SCHEMAS, make_group(reports.VALID), attachment_count=2)
    missing = {i.field_id for i in result.issues if i.code == "field_missing"}
    assert "promotion_link" not in missing


def test_labels_without_numbers_still_bind() -> None:
    result = parse_report(SCHEMAS, make_group(reports.NO_NUMBERS))
    formation = result.field_value("formation_idn")
    assert formation is not None
    assert formation["idn_number"] == "0016"


def test_dash_markers_do_not_satisfy_evidence() -> None:
    result = parse_report(SCHEMAS, make_group(reports.DASH_MARKERS), attachment_count=0)
    missing = {i.field_id for i in result.issues if i.code == "field_missing"}
    assert "evidence_1" in missing


def test_parse_output_is_diagnosable() -> None:
    """A staff member must be able to see why a report was judged malformed."""
    result = parse_report(SCHEMAS, make_group(reports.REAL_LORE_IDN))
    assert "scores" in result.diagnostics
    assert "bindings" in result.diagnostics
    bindings = {b["field"]: b for b in result.diagnostics["bindings"]}
    assert bindings["formation_idn"]["present"] is True


def test_schema_is_serialisable_for_storage() -> None:
    schema = training_report_schema()
    definition = schema.to_definition()
    assert definition["key"] == "training_report"
    assert isinstance(definition["fields"], list)
    assert definition["fields"][0]["id"] == "formation_idn"


def test_nested_evidence_fields_resolve_from_template_paths() -> None:
    result = parse_report(SCHEMAS, make_group(reports.VALID), attachment_count=2)
    bindings = {b.spec.id: b for b in result.fields}
    assert bindings["evidence_1"].spec.path == "3.1"
    assert bindings["evidence_2"].spec.path == "3.2"

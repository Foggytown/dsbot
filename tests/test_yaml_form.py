"""Tests for the YAML form format and multi-condition rules.

The motivating case is the real training form: photos depend on the qualification, and
the previous-report link is required only for non-T qualifications. Neither can be
expressed by a single field, so rules exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
from dsbot.modules.reports.schema import FormSchemaDef
from dsbot.modules.reports.yaml_form import (
    compile_problems,
    form_to_yaml,
    load_yaml,
    parse_form_file,
)

FORM_PATH = Path("forms/training.yaml")


def load_training() -> FormSchemaDef:
    return parse_form_file(FORM_PATH)


def group(content: str, attachments: int = 0) -> ReportCandidate:
    return ReportCandidate(
        messages=[
            GroupMessage(
                message_id=1,
                author_id=42,
                created_at=datetime.now(UTC),
                content=content,
                attachment_count=attachments,
            )
        ]
    )


def codes(schema: FormSchemaDef, content: str, attachments: int) -> dict[str, str]:
    result = parse_report([schema], group(content, attachments), attachment_count=attachments)
    return {i.field_id or "?": i.code for i in result.issues}


# --- the file itself -------------------------------------------------------


def test_training_form_parses() -> None:
    schema = load_training()
    assert schema.key == "training_report"
    assert {f.id for f in schema.fields} == {
        "identity",
        "qualification",
        "photos",
        "previous_report",
    }


def test_training_form_has_no_problems() -> None:
    assert compile_problems(load_training()) == []


def test_type_aliases_are_accepted() -> None:
    """`attachment` and `link` are friendlier than the internal type names."""
    fields = {f.id: f for f in load_training().fields}
    assert fields["photos"].type == "attachment_required"
    assert fields["previous_report"].type == "discord_message_ref"


def test_yaml_roundtrip() -> None:
    original = load_training()
    rendered = form_to_yaml(original)
    temp = Path("var/_roundtrip.yaml")
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(rendered, encoding="utf-8")
    try:
        restored = parse_form_file(temp)
    finally:
        temp.unlink(missing_ok=True)
    assert restored.key == original.key
    assert [f.id for f in restored.fields] == [f.id for f in original.fields]
    assert len(restored.rules) == len(original.rules)


def test_missing_file_raises_readable_error() -> None:
    with pytest.raises(FileNotFoundError):
        load_yaml(Path("forms/does-not-exist.yaml"))


def test_rule_referring_to_unknown_field_is_reported() -> None:
    data = load_yaml(FORM_PATH)
    data["rules"][0]["field"] = "nope"
    problems = compile_problems(FormSchemaDef.model_validate(data))
    assert any("unknown field 'nope'" in p for p in problems)


def test_rule_condition_on_unknown_field_is_reported() -> None:
    data = load_yaml(FORM_PATH)
    data["rules"][0]["when"][0]["field"] = "ghost"
    problems = compile_problems(FormSchemaDef.model_validate(data))
    assert any("ghost" in p for p in problems)


def test_rule_without_message_is_reported() -> None:
    data = load_yaml(FORM_PATH)
    data["rules"][0].pop("message")
    problems = compile_problems(FormSchemaDef.model_validate(data))
    assert any("add a message" in p for p in problems)


# --- the actual rules ------------------------------------------------------

BY_ID = None

REPORT_T = """1. SOB | 0016
2. T
3. фото
4."""
REPORT_INF = """1. SOB | 0016
2. INF
3. фото
4. https://discord.com/channels/1/2/3"""


def test_t_qualification_with_exactly_one_photo_is_valid() -> None:
    schema = load_training()
    result = parse_report([schema], group(REPORT_T, 1), attachment_count=1)
    assert result.is_valid, [i.to_payload() for i in result.issues]


def test_t_qualification_with_two_photos_is_invalid() -> None:
    """T requires exactly one photo, so two is also wrong."""
    schema = load_training()
    result = parse_report([schema], group(REPORT_T, 2), attachment_count=2)
    assert not result.is_valid
    assert "rule_exact_attachments" in {i.code for i in result.issues}


def test_t_qualification_with_no_photo_is_invalid() -> None:
    schema = load_training()
    result = parse_report([schema], group(REPORT_T, 0), attachment_count=0)
    assert not result.is_valid


def test_t_qualification_needs_no_link() -> None:
    schema = load_training()
    result = parse_report([schema], group(REPORT_T, 1), attachment_count=1)
    assert "rule_required" not in {i.code for i in result.issues}


def test_other_qualification_needs_two_photos() -> None:
    """Real requirement: 2+ photos unless the qualification is T."""
    schema = load_training()
    result = parse_report([schema], group(REPORT_INF, 1), attachment_count=1)
    assert "rule_min_attachments" in {i.code for i in result.issues}
    assert not result.is_valid


def test_other_qualification_with_three_photos_is_valid() -> None:
    """Non-T needs at least two, so three is fine."""
    schema = load_training()
    result = parse_report([schema], group(REPORT_INF, 3), attachment_count=3)
    assert result.is_valid, [i.to_payload() for i in result.issues]


def test_rule_bounds_are_validated() -> None:
    """min > max is a configuration mistake and must be rejected at load time."""
    data = load_yaml(FORM_PATH)
    data["rules"][0]["min_attachments"] = 3
    data["rules"][0]["max_attachments"] = 1
    with pytest.raises(ValueError, match="greater than max_attachments"):
        FormSchemaDef.model_validate(data)


def test_other_qualification_without_link_is_invalid() -> None:
    """Real requirement: link required unless the qualification is T."""
    schema = load_training()
    content = "1. SOB | 0016\n2. INF\n3. фото\n4."
    result = parse_report([schema], group(content, 2), attachment_count=2)
    assert "rule_required" in {i.code for i in result.issues}
    assert not result.is_valid


def test_rule_message_is_human_readable() -> None:
    schema = load_training()
    content = "1. SOB | 0016\n2. INF\n3. фото\n4."
    result = parse_report([schema], group(content, 2), attachment_count=2)
    issues = [i for i in result.issues if i.code == "rule_required"]
    assert issues
    assert "ссылк" in issues[0].detail["message"].lower()


# --- identity field -------------------------------------------------------

IDENTITY_FIELD = load_training().field_by_id("identity")
FORMATIONS = IDENTITY_FIELD.formations  # type: ignore[union-attr]


def identity(text: str) -> dict:
    from dsbot.modules.reports.fields import parse_identity

    result = parse_identity(text, "identity", FORMATIONS)
    return {
        "value": result.value,
        "codes": [i.code for i in result.issues],
    }


def test_identity_plain_idn() -> None:
    """The normal case: formation plus a 4-digit IDN."""
    parsed = identity("SOB | 0016")
    assert parsed["value"]["formation"] == "SOB"
    assert parsed["value"]["lore_idn"] == "0016"
    assert parsed["codes"] == []


def test_identity_with_numeric_prefix() -> None:
    """`01/0016` is a normal lore IDN. The bot does not care what the 01 means."""
    parsed = identity("SOB | 01/0016")
    assert parsed["value"]["formation"] == "SOB"
    assert parsed["value"]["prefix"] == "01"
    assert parsed["value"]["lore_idn"] == "0016"
    assert parsed["codes"] == []


def test_identity_with_optional_nrp() -> None:
    parsed = identity("SOB | 01/0016 | 0017")
    assert parsed["value"]["lore_idn"] == "0016"
    assert parsed["value"]["nrp_idn"] == "0017"
    assert parsed["codes"] == []


def test_identity_plain_can_also_carry_nrp() -> None:
    parsed = identity("SOB | 0016 | 0017")
    assert parsed["value"]["lore_idn"] == "0016"
    assert parsed["value"]["nrp_idn"] == "0017"


def test_squad_names_are_not_accepted_as_formations() -> None:
    """ALPHA is a squad, not a formation; reporters must write SOB."""
    parsed = identity("ALPHA | 01/0016")
    assert "formation_unknown" in parsed["codes"]


def test_identity_unknown_formation() -> None:
    parsed = identity("ZZZ | 0016")
    assert "formation_unknown" in parsed["codes"]


def test_identity_bad_length() -> None:
    parsed = identity("SOB | 016")
    assert "idn_bad_length" in parsed["codes"]


def test_six_digit_idn_allowed_as_lore() -> None:
    parsed = identity("SOB | 000016")
    assert parsed["value"]["lore_idn"] == "000016"
    assert parsed["codes"] == []


def test_six_digit_lore_plus_four_digit_nrp_is_valid() -> None:
    """A lore IDN may be 6 digits; the system IDN after it must still be 4."""
    parsed = identity("SOB | 000016 | 0017")
    assert parsed["value"]["lore_idn"] == "000016"
    assert parsed["value"]["nrp_idn"] == "0017"
    assert parsed["codes"] == []


def test_system_idn_must_be_exactly_four_digits() -> None:
    """A 6-digit value in the system slot is a lore IDN written in the wrong place."""
    parsed = identity("SOB | 0016 | 000017")
    assert parsed["value"]["nrp_idn"] == "000017"
    assert "nrp_bad_length" in parsed["codes"]
    assert "idn_bad_length" not in parsed["codes"]


def test_system_idn_of_three_digits_is_rejected() -> None:
    parsed = identity("SOB | 0016 | 017")
    assert "nrp_bad_length" in parsed["codes"]


def test_lore_and_system_lengths_report_different_rules() -> None:
    """The two positions have different rules, so they must give different messages."""
    from dsbot.modules.reports.templates import issue_line

    lore = identity("SOB | 016")
    nrp = identity("SOB | 0016 | 017")
    assert lore["codes"] == ["idn_bad_length"]
    assert nrp["codes"] == ["nrp_bad_length"]

    lore_text = issue_line("idn_bad_length", "ru", detail_raw="016")
    nrp_text = issue_line("nrp_bad_length", "ru", detail_raw="017")
    assert "4 цифры или 6" in lore_text
    assert "ровно 4 цифры" in nrp_text


def test_identity_missing_formation_warns() -> None:
    parsed = identity("0016")
    assert "idn_no_formation" in parsed["codes"]

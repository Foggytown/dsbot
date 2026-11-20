"""Strict identity parsing, verified against a real scan of the production channel.

The rule is simple: the template is the only accepted format. These values feed weekly
reports, so tolerance would silently produce wrong statistics. Anything that deviates is
rejected with a message saying exactly what to change.

Accepted:

    ARC | 0042
    SOB | 01/0016 | 0017

Rejected: everything else, including the variants that once seemed reasonable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dsbot.modules.reports.fields import parse_enum, parse_identity
from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
from dsbot.modules.reports.yaml_form import parse_form_file

FORM = parse_form_file(Path("forms/training.yaml"))
IDENTITY = FORM.field_by_id("identity")
FORMATIONS = IDENTITY.formations  # type: ignore[union-attr]
PREFIXES = IDENTITY.lore_prefixes  # type: ignore[union-attr]
PREFIX_FORMATIONS = IDENTITY.lore_prefix_formations  # type: ignore[union-attr]


def ident(text: str) -> tuple[dict, list[str]]:
    result = parse_identity(text, "identity", FORMATIONS, PREFIXES, PREFIX_FORMATIONS)
    return result.value or {}, [i.code for i in result.issues]


# --- the two accepted shapes ----------------------------------------------


def test_plain_idn_is_accepted() -> None:
    for text in ("ARC | 0042", "CT | 8096", "SOB | 0016", "212th | 6489"):
        value, codes = ident(text)
        assert codes == [], text
        assert value["prefix"] is None


def test_prefixed_with_system_idn_is_accepted() -> None:
    value, codes = ident("SOB | 01/0016 | 0017")
    assert codes == []
    assert value["prefix"] == "01"
    assert value["lore_idn"] == "0016"
    assert value["nrp_idn"] == "0017"


def test_prefix_00_is_accepted() -> None:
    value, codes = ident("SOB | 00/0087 | 9001")
    assert codes == []
    assert value["prefix"] == "00"


def test_formation_case_must_match_the_template() -> None:
    """The template spelling is the specification: Guard, Jedi, 212th, 501st."""
    for text in ("Guard | 3280", "Jedi | 1001", "212th | 6489", "501st | 2641"):
        _value, codes = ident(text)
        assert codes == [], text


def test_all_caps_is_rejected() -> None:
    """GUARD and JEDI are mistakes, not variants: the report data must be uniform."""
    for text in ("GUARD | 3280", "JEDI | 1001", "212TH | 6489", "501ST | 2641"):
        _value, codes = ident(text)
        assert "formation_unknown" in codes, text


def test_lowercase_formation_is_rejected() -> None:
    for text in ("sob | 0016", "arc | 0042", "ct | 8096"):
        _value, codes = ident(text)
        assert "formation_unknown" in codes, text


def test_allowed_list_uses_template_spelling() -> None:
    _value, codes = ident("ZZZ | 0016")
    assert "list_allowed_formations" in codes


# --- everything else is an error ------------------------------------------


def test_prefixed_idn_requires_system_idn() -> None:
    _value, codes = ident("SOB | 01/0038")
    assert "nrp_missing" in codes


def test_plain_idn_with_second_idn_is_valid() -> None:
    """Some soldiers have both a lore IDN and a system IDN; both are 4 digits."""
    value, codes = ident("SOB | 0016 | 0017")
    assert codes == []
    assert value["lore_idn"] == "0016"
    assert value["nrp_idn"] == "0017"


def test_two_plain_idns_work_for_every_formation() -> None:
    """`FORMATION | IDN | IDN` is general; only 01/ and 00/ are SOB-specific."""
    for formation in ("SOB", "ARC", "CT", "Jedi", "212th", "501st"):
        value, codes = ident(f"{formation} | 0042 | 0043")
        assert codes == [], formation
        assert value["nrp_idn"] == "0043"


def test_second_idn_must_still_be_four_digits() -> None:
    _value, codes = ident("ARC | 0042 | 043")
    assert "nrp_bad_length" in codes


def test_prefix_is_sob_only() -> None:
    _value, codes = ident("ARC | 01/0016 | 0000")
    assert "lore_prefix_not_allowed_for_formation" in codes


def test_prefix_must_be_01_or_00() -> None:
    _value, codes = ident("SOB | 02/0016 | 0000")
    assert "lore_prefix_invalid" in codes


def test_prefixed_body_must_start_with_00() -> None:
    _value, codes = ident("SOB | 01/0116 | 0000")
    assert "idn_bad_shape" in codes


def test_plain_idn_must_be_four_digits() -> None:
    for text in ("ARC | 42", "ARC | 004200", "ARC | 042"):
        _value, codes = ident(text)
        assert "idn_bad_shape" in codes, text


def test_backslash_is_rejected() -> None:
    _value, codes = ident("SOB | 01\\0016 | 0000")
    assert "backslash_not_allowed" in codes


def test_brackets_are_rejected() -> None:
    _value, codes = ident("SOB | 01/0038 [8289]")
    assert "brackets_not_allowed" in codes


def test_extra_word_in_formation_is_rejected() -> None:
    """`ARC Alpha` is not a formation name."""
    _value, codes = ident("ARC Alpha | 01/0041 | 0000")
    assert "formation_not_single_word" in codes


def test_missing_pipe_is_rejected() -> None:
    _value, codes = ident("ARC 0085")
    assert "identity_bad_format" in codes


def test_multiple_soldiers_with_comma_are_all_parsed() -> None:
    """Several soldiers in field 1 are allowed, separated by `,`, each checked."""
    value, codes = ident("ARC | 7784, ARC | 8440")
    assert codes == []
    assert value["extra_identities"] is True
    assert value["soldier_count"] == 2
    assert value["all_identities"][0]["lore_idn"] == "7784"
    assert value["all_identities"][1]["lore_idn"] == "8440"


def test_each_soldier_is_checked_independently() -> None:
    """One bad soldier in the list still fails, and the good one is kept."""
    value, codes = ident("ARC | 7784, ARC | 84")
    assert "idn_bad_shape" in codes
    assert value["all_identities"][0]["lore_idn"] == "7784"


def test_semicolon_is_told_to_use_a_comma() -> None:
    """`;` is a common separator, but the template uses `,`; say so directly."""
    _value, codes = ident("ARC | 7784 ; ARC | 8440")
    assert "semicolon_not_allowed" in codes
    # No confusing follow-on errors about the IDN shape.
    assert "idn_bad_shape" not in codes


def test_semicolon_warning_text_names_the_comma() -> None:
    from dsbot.modules.reports.templates import issue_line

    text = issue_line("semicolon_not_allowed", "ru")
    assert "," in text
    assert ";" in text


def test_comma_separated_soldiers_remain_valid() -> None:
    _value, codes = ident("ARC | 7784, ARC | 8440")
    assert codes == []


def test_labelled_idns_are_rejected() -> None:
    _value, codes = ident("SOB | NRP-4113")
    assert "idn_labelled_not_allowed" in codes


def test_name_instead_of_number_is_rejected() -> None:
    _value, codes = ident("212th | Revers")
    assert "idn_bad_shape" in codes


def test_unknown_formation_lists_allowed_values() -> None:
    _value, codes = ident("99 | 9902")
    assert "formation_unknown" in codes
    assert "list_allowed_formations" in codes


def test_501th_is_not_501st() -> None:
    """English ordinals: 501st, never 501th."""
    _value, codes = ident("501th | 2641")
    assert "formation_unknown" in codes
    _value, codes = ident("501TH | 2641")
    assert "formation_unknown" in codes


def test_501st_is_stored_in_canonical_spelling() -> None:
    """Written exactly as the template says, it is stored unchanged."""
    value, codes = ident("501st | 2641")
    assert codes == []
    assert value["formation"] == "501st"


def test_99th_is_valid() -> None:
    _value, codes = ident("99th | 9902")
    assert codes == []


# --- qualifications --------------------------------------------------------


def test_qualification_with_explanation_is_matched() -> None:
    result = parse_enum(
        "D-U-C (Инструкторская система повышения)",
        "qualification",
        FORM.options_of("qualification"),
    )
    assert result.value is not None
    assert result.value["code"] == "D-U-C"
    assert not result.issues


def test_lowercase_qualification_is_matched() -> None:
    result = parse_enum("t", "qualification", FORM.options_of("qualification"))
    assert result.value is not None
    assert result.value["code"] == "T"


def test_unknown_qualification_lists_the_options() -> None:
    result = parse_enum("pepe", "qualification", FORM.options_of("qualification"))
    codes = [i.code for i in result.issues]
    assert "enum_unknown_option" in codes
    assert "list_allowed_qualifications" in codes


# --- end to end ------------------------------------------------------------


def group(content: str, attachments: int = 0) -> ReportCandidate:
    return ReportCandidate(
        messages=[
            GroupMessage(
                message_id=1,
                author_id=1,
                created_at=datetime.now(UTC),
                content=content,
                attachment_count=attachments,
            )
        ]
    )


def test_warning_pings_the_author_without_a_nickname() -> None:
    """The author is pinged plainly; the nickname belongs in scan logs, not warnings."""
    from dsbot.modules.reports.service import ReportService

    schema = FORM
    content = "1. ARC | 0042\n2. pepe\n3."
    result = parse_report([schema], group(content, 0))

    service = ReportService.__new__(ReportService)
    service.contact_user_id = 999
    body = service._compose_warning("ru", result, 42)

    assert body.startswith("<@42>")
    assert "`" not in body.splitlines()[0]
    assert "<@999>" in body


def test_warning_points_to_the_pinned_message_instead_of_repeating_the_form() -> None:
    from dsbot.modules.reports.service import ReportService

    schema = FORM
    result = parse_report([schema], group("1. ARC | 0042\n2. pepe\n3.", 0))

    service = ReportService.__new__(ReportService)
    service.contact_user_id = None
    body = service._compose_warning("ru", result, 42)

    assert "закреплённом сообщении" in body
    # No code block reproducing the form.
    assert "```" not in body


def test_rules_do_not_fire_when_the_qualification_is_unrecognised() -> None:
    """An unknown qualification must not also produce the 2-photo and link complaints."""
    schema = FORM
    result = parse_report([schema], group("1. ARC | 0042\n2. pepe\n3. фото", 1))
    codes = {i.code for i in result.issues}
    assert "enum_unknown_option" in codes
    assert "rule_min_attachments" not in codes
    assert "rule_required" not in codes


def test_missing_photo_is_reported_even_with_unknown_qualification() -> None:
    """ "At least one photo" applies to the form, not to the qualification."""
    schema = FORM
    result = parse_report([schema], group("1. ARC | 0042\n2. pepe\n3.", 0))
    codes = {i.code for i in result.issues}
    assert "field_missing" in codes
    missing = next(i for i in result.issues if i.code == "field_missing")
    assert missing.field_id == "photos"


def test_warning_pings_the_contact_user() -> None:
    """The last line must actually ping, not print a dash."""
    from dsbot.modules.reports.service import ReportService

    schema = FORM
    result = parse_report([schema], group("1. ARC | 0042\n2. pepe\n3.", 0))

    service = ReportService.__new__(ReportService)
    service.contact_user_id = 123456789
    body = service._compose_warning("ru", result, 42)
    assert "<@123456789>" in body


def test_contact_ping_comes_from_settings() -> None:
    from dsbot.config import Settings

    assert Settings().contact_user_id is not None


def test_rules_do_fire_for_a_recognised_non_t_qualification() -> None:
    schema = FORM
    result = parse_report([schema], group("1. ARC | 0042\n2. INF\n3.", 0))
    codes = {i.code for i in result.issues}
    assert "rule_required" in codes


def test_absent_photos_are_reported_once() -> None:
    """`field_missing` already says it; do not also say "found 0"."""
    schema = FORM
    result = parse_report([schema], group("1. ARC | 0042\n2. INF\n3.", 0))
    codes = [i.code for i in result.issues]
    assert codes.count("field_missing") == 1
    assert "rule_min_attachments" not in codes


def test_field_label_is_used_not_the_long_hint() -> None:
    """A hint sentence is unreadable inside «...», so the short label is used."""
    from dsbot.modules.reports.service import _field_hint

    schema = FORM
    result = parse_report([schema], group("1. ARC | 0042\n2. INF\n3.", 0))
    hint = _field_hint("photos", result)
    assert hint == "доказательства"
    assert len(hint) < 40


def test_correct_arc_report_is_valid() -> None:
    content = (
        "1. ARC | 7404\n"
        "2. D-U-C (Инструкторская система повышения)\n"
        "3.\n"
        "4. https://discord.com/channels/1/2/3"
    )
    result = parse_report([FORM], group(content, 4), attachment_count=4)
    assert result.is_valid, [i.to_payload() for i in result.issues]


def test_correct_sob_report_is_valid() -> None:
    content = "1. SOB | 01/0095 | 8076\n2. T\n3."
    result = parse_report([FORM], group(content, 1), attachment_count=1)
    assert result.is_valid, [i.to_payload() for i in result.issues]

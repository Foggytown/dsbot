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


def test_formation_case_is_ignored() -> None:
    for text in ("212th | 6489", "212TH | 6489", "212Th | 6489"):
        _value, codes = ident(text)
        assert codes == [], text


def test_cyrillic_lookalikes_are_folded() -> None:
    """`СT` with a Cyrillic es is unmistakably CT, so it is not a spelling mistake."""
    _value, codes = ident("СT | 5553")
    assert codes == []
    _value, codes = ident("СТ | 5553")
    assert codes == []


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
    for formation in ("SOB", "ARC", "CT", "JEDI", "212th", "501st"):
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


def test_semicolon_is_not_a_separator() -> None:
    """The separator is `,`, not `;`."""
    _value, codes = ident("ARC | 7784 ; ARC | 8440")
    assert codes != []


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
    """However the reporter capitalises it, the stored value matches the form."""
    for text in ("501st | 2641", "501ST | 2641", "501St | 2641"):
        value, codes = ident(text)
        assert codes == [], text
        assert value["formation"] == "501st", text


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

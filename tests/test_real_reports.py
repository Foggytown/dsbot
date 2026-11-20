"""Regression tests built from a real scan of the production channel.

Every case here is a message an instructor actually posted. Each one either failed
wrongly, or was parsed in a way that hid the real mistake.
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


# --- second IDN must not be rejected ---------------------------------------


def test_bracketed_second_idn_is_rejected() -> None:
    """Brackets are not part of the template: the separator is `|`.

    The IDNs are still read correctly, so the warning is about the brackets only.
    """
    value, codes = ident("SOB | 01/0038 [8289]")
    assert "brackets_not_allowed" in codes
    assert value["prefix"] == "01"
    assert value["lore_idn"] == "0038"


def test_pipe_separated_second_idn_is_accepted() -> None:
    value, codes = ident("SOB | 01/0038 | 8289")
    assert codes == []
    assert value["prefix"] == "01"
    assert value["lore_idn"] == "0038"
    assert value["nrp_idn"] == "8289"


def test_backslash_separator_is_accepted() -> None:
    """`01\\0095` uses a backslash; reporters type it constantly."""
    value, codes = ident("SOB | 01\\0095 | 8076")
    assert codes == []
    assert value["prefix"] == "01"
    assert value["lore_idn"] == "0095"


def test_plain_plus_second_idn_is_accepted() -> None:
    value, codes = ident("SOB | 1262 | 4313")
    assert codes == []
    assert value["lore_idn"] == "1262"
    assert value["nrp_idn"] == "4313"


def test_labelled_idns_are_accepted() -> None:
    value, codes = ident("SOB | NRP-4113 | RP-1262 | RC-62")
    assert codes == []
    assert value["lore_idn"] == "4113"


def test_numbers_in_any_order() -> None:
    """`ARC | (8377) 01\\0016` puts the plain IDN first."""
    value, codes = ident("ARC | (8377) 01\\0016")
    # A prefix on ARC is its own error, but the IDNs themselves must be read correctly.
    assert value["prefix"] == "01"
    assert value["lore_idn"] == "0016"
    assert "idn_bad_shape" not in codes


def test_multiple_soldiers_are_kept() -> None:
    value, codes = ident("ARC | 7784 ; ARC | 8440")
    assert codes == []
    assert value["extra_identities"] is True
    assert len(value["all_identities"]) == 2


# --- formation problems must be reported as formation problems --------------


def test_unknown_formation_is_not_reported_as_bad_idn() -> None:
    """`99 | 9904` is a wrong FORMATION; the IDN `9904` itself is fine."""
    _value, codes = ident("99 | 9904")
    assert "formation_unknown" in codes
    assert "lore_prefix_invalid" not in codes


def test_unknown_formation_lists_allowed_values() -> None:
    _value, codes = ident("ZZZ | 0016")
    assert "list_allowed_formations" in codes


def test_stray_word_does_not_become_part_of_formation() -> None:
    """`ARC Alpha` is formation ARC plus noise, not "ARC ALPHA"."""
    value, _codes = ident("ARC Alpha | 01/0041 | 8341")
    assert value["formation"] == "ARC"


def test_no_pipe_still_parses_formation_and_idn() -> None:
    """`ARC 0085` has no separator at all."""
    value, codes = ident("ARC 0085")
    assert value["formation"] == "ARC"
    assert value["lore_idn"] == "0085"
    assert codes == []


# --- case and look-alike letters ------------------------------------------


def test_formation_case_is_insensitive() -> None:
    for text in ("212th | 6489", "212TH | 6489", "212Th | 6489"):
        value, codes = ident(text)
        assert value["formation"] == "212TH"
        assert codes == [], text


def test_cyrillic_lookalikes_are_folded() -> None:
    """`СT` with a Cyrillic es is the same formation as `CT`."""
    value, codes = ident("СT | 5553")
    assert value["formation"] == "CT"
    assert codes == []


def test_cyrillic_both_letters() -> None:
    value, codes = ident("СТ | 5553")
    assert value["formation"] == "CT"
    assert codes == []


def test_501st_is_the_canonical_spelling() -> None:
    value, codes = ident("501st | 8995")
    assert value["formation"] == "501ST"
    assert codes == []


def test_501th_is_rejected() -> None:
    """A different string: 501th must not be accepted as 501st."""
    _value, codes = ident("501th | 2641")
    assert "formation_unknown" in codes


def test_99th_is_a_valid_formation() -> None:
    _value, codes = ident("99th | 9902")
    assert codes == []


# --- qualification commentary ---------------------------------------------


def test_qualification_with_explanation_is_matched() -> None:
    """`D-U-C (Инструкторская система повышения)` means D-U-C."""
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


# --- end-to-end on a real message -----------------------------------------


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


def test_real_arc_report_is_valid() -> None:
    content = (
        "1. ARC | 7404\n"
        "2. D-U-C (Инструкторская система повышения)\n"
        "3.\n"
        "4. https://discord.com/channels/1/2/3"
    )
    result = parse_report([FORM], group(content, 4), attachment_count=4)
    assert result.is_valid, [i.to_payload() for i in result.issues]


def test_real_sob_report_with_brackets_is_flagged() -> None:
    """Real message, but brackets are not the template format, so it must warn."""
    content = "1. SOB | 01/0095 [8076]\n2. T\n3."
    result = parse_report([FORM], group(content, 1), attachment_count=1)
    codes = {i.code for i in result.issues}
    assert "brackets_not_allowed" in codes


def test_real_sob_report_with_pipe_is_valid() -> None:
    """The same report written correctly."""
    content = "1. SOB | 01/0095 | 8076\n2. T\n3."
    result = parse_report([FORM], group(content, 1), attachment_count=1)
    assert result.is_valid, [i.to_payload() for i in result.issues]

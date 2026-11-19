"""Field parser tests: the typed values extracted from report text."""

from __future__ import annotations

from dsbot.modules.reports.fields import (
    parse_attachment,
    parse_count_fraction,
    parse_discord_link,
    parse_enum,
    parse_formation_idn,
)


def test_formation_with_lore_idn() -> None:
    result = parse_formation_idn("SOB | 01/0016", "formation_idn")
    assert result.present
    assert result.value is not None
    assert result.value["formation"] == "SOB"
    assert result.value["idn_kind"] == "rp_lore"
    assert result.value["sub_unit"] == "01"
    assert result.value["idn_number"] == "0016"
    assert not result.issues


def test_formation_with_plain_nrp() -> None:
    result = parse_formation_idn("SOB | 0016", "formation_idn")
    assert result.value is not None
    assert result.value["idn_kind"] == "nrp"
    assert result.value["sub_unit"] is None
    assert result.value["idn_number"] == "0016"


def test_idn_is_zero_padded_for_comparison() -> None:
    result = parse_formation_idn("ARC | 16", "formation_idn")
    assert result.value is not None
    assert result.value["idn_number"] == "0016"


def test_idn_collision_is_not_an_error() -> None:
    """>10k characters share four digits; the same IDN twice must parse cleanly."""
    first = parse_formation_idn("SOB | 0016", "formation_idn")
    second = parse_formation_idn("SOB | 0016", "formation_idn")
    assert first.value == second.value
    assert not first.issues and not second.issues


def test_formation_missing_is_a_warning_not_a_failure() -> None:
    result = parse_formation_idn("0016", "formation_idn")
    assert result.present
    assert any(i.code == "idn_no_formation" for i in result.issues)
    assert all(i.severity == "warning" for i in result.issues)


def test_formation_blank_is_absent() -> None:
    assert not parse_formation_idn("", "formation_idn").present
    assert not parse_formation_idn("-", "formation_idn").present


def test_enum_matches_case_and_spaces() -> None:
    result = parse_enum("inf +", "qualification", ["INF", "INF+", "ENG"])
    assert result.value is not None
    assert result.value["matched"] is True
    assert result.value["code"] == "INF+"


def test_enum_unknown_is_a_warning() -> None:
    result = parse_enum("XYZ", "qualification", ["INF", "ENG"])
    assert result.present
    assert any(i.code == "enum_unknown_option" for i in result.issues)
    assert all(i.severity == "warning" for i in result.issues)


def test_discord_link_parsed_into_ids() -> None:
    url = "https://discord.com/channels/1274402776696488108/1485791744917241938/1570351536259731550"
    result = parse_discord_link(url, "promotion_link")
    assert result.value is not None
    assert result.value["guild_id"] == 1274402776696488108
    assert result.value["message_id"] == 1570351536259731550


def test_non_discord_link_is_a_warning() -> None:
    result = parse_discord_link("https://example.com/foo", "promotion_link")
    assert result.present
    assert any(i.code == "link_not_discord" for i in result.issues)


def test_attachment_with_file_present() -> None:
    result = parse_attachment("", "evidence_1", attachment_count=1)
    assert result.present
    assert result.value is not None
    assert result.value["attachment_count"] == 1


def test_attachment_missing_file_is_absent_not_error() -> None:
    """No file yet: the photo may still arrive in a follow-up message."""
    result = parse_attachment("", "evidence_1", attachment_count=0)
    assert not result.present
    assert not result.issues


def test_fraction_parsed() -> None:
    result = parse_count_fraction("8/8", "tests")
    assert result.value is not None
    assert result.value["got"] == 8
    assert result.value["total"] == 8


def test_fraction_impossible_is_an_error() -> None:
    result = parse_count_fraction("9/8", "tests")
    assert any(i.code == "fraction_invalid" for i in result.issues)
    assert any(i.severity == "error" for i in result.issues)

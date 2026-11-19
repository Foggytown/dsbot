"""Tokenizer tests. These encode the tolerance rules the channel actually needs."""

from __future__ import annotations

from dsbot.modules.reports.text import (
    extract_urls,
    is_blank_value,
    normalize_label,
    tokenize,
)
from tests.fixtures import reports


def test_parses_plain_numbered_lines() -> None:
    result = tokenize(reports.REAL_LORE_IDN)
    first = result.item_at((1,))
    assert first is not None
    assert result.item_at((2,)) is not None
    assert result.item_at((3,)) is not None
    assert result.item_at((4,)) is not None
    assert first.value == "SOB | 01/0016"


def test_empty_item_is_blank() -> None:
    result = tokenize(reports.REAL_LORE_IDN)
    item = result.item_at((3,))
    assert item is not None
    assert item.value == ""
    assert item.is_blank


def test_dash_marker_counts_as_blank() -> None:
    result = tokenize(reports.DASH_MARKERS)
    item = result.item_at((3,))
    assert item is not None
    assert item.is_blank


def test_nested_bullets_get_subpaths() -> None:
    result = tokenize(reports.VALID)
    children = result.children_of(3)
    assert len(children) == 2
    assert children[0].path == (3, 1)
    assert children[1].path == (3, 2)


def test_skipped_numbers_do_not_shift_others() -> None:
    """Dropping item 2 entirely must not make item 3 look like item 2."""
    text = "1. SOB | 0016\n3. photo"
    result = tokenize(text)
    assert result.item_at((1,)) is not None
    assert result.item_at((2,)) is None
    assert result.item_at((3,)) is not None


def test_bold_markers_are_stripped() -> None:
    result = tokenize("**1.** SOB | 0016\n**2.** INF")
    first = result.item_at((1,))
    assert first is not None
    assert "SOB" in first.value


def test_continuation_line_joins_previous_item() -> None:
    text = "1. SOB | 0016\n   continued here\n2. INF"
    result = tokenize(text)
    item = result.item_at((1,))
    assert item is not None
    assert "continued here" in item.value


def test_preamble_is_kept() -> None:
    result = tokenize("Заголовок\n1. SOB | 0016")
    assert "Заголовок" in result.preamble


def test_unparsed_prose_is_not_an_item() -> None:
    result = tokenize(reports.GARBAGE)
    assert result.items == ()
    assert "тренировали" in result.preamble


def test_url_extraction_handles_markdown_and_bare() -> None:
    text = "see [link](https://discord.com/channels/1/2/3) and <https://discord.com/channels/1/2/4>"
    urls = extract_urls(text)
    assert "https://discord.com/channels/1/2/3" in urls
    assert "https://discord.com/channels/1/2/4" in urls


def test_blank_value_variants() -> None:
    for value in ["", " ", "-", "—", "n/a", "нет", ".", " : "]:
        assert is_blank_value(value), value
    assert not is_blank_value("INF")


def test_normalize_label_collapses_punctuation() -> None:
    assert normalize_label("Название квалификации:") == "название квалификации"
    assert normalize_label("  ФОТО   в инж. блоке ") == "фото в инж блоке"


def test_template_markers_are_recognisable() -> None:
    result = tokenize(reports.TEMPLATE)
    # The template nests its own numbering, so items exist; disqualification
    # happens at schema scoring, not tokenisation.
    assert result.items

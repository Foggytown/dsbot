"""Text normalisation and structural tokenisation.

Reports are numbered lists typed by humans. The tokeniser's job is to recover the
*structure* (which item is which) while tolerating:
  * `1.` / `1)` / `1:` / `1` markers, optionally bold
  * bullets under a numbered item, nested by indentation
  * a marker with no value (`3.`), a dash (`3. -`), or whitespace
  * numbers skipped entirely
  * values that continue on the next line
  * markdown links and angle-bracket URLs
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

# A numbered marker: "1", "3.1", "1." "2)" "4:" with optional trailing punctuation.
_NUMBER_RE: Final = re.compile(
    r"^(?P<marker>\d{1,2}(?:\.\d{1,2}){0,2})\s*(?:[.):\u2013\u2014-](?!\d)|\s)\s*(?P<value>.*)$"
)
_BULLET_RE: Final = re.compile(r"^(?P<bullet>[-\u2022*\u00b7\u2013\u2014])\s+(?P<value>.*)$")
_BLANK_MARKERS: Final = frozenset({"-", "\u2013", "\u2014", "\u2022", "*", ".", ":", "n/a", "нет"})
_MD_EMPHASIS: Final = re.compile(r"(\*\*|__|\*|_|`)(?=\S)|(?<=\S)(\*\*|__|\*|_|`)")
_URL_RE: Final = re.compile(r"https?://[^\s<>()\[\]\"']+")

_ZERO_WIDTH: Final = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)


def normalize(text: str) -> str:
    """Canonicalise unicode noise without destroying structure."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ZERO_WIDTH)
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    return text


def strip_emphasis(text: str) -> str:
    """Remove markdown emphasis characters, keeping the inner text."""
    return _MD_EMPHASIS.sub("", text)


def normalize_label(text: str) -> str:
    """Aggressive normalisation used for label comparison only."""
    text = normalize(text).lower()
    text = strip_emphasis(text)
    text = re.sub(r"[^\w\s+/|-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_blank_value(value: str) -> bool:
    cleaned = strip_emphasis(normalize(value)).strip().strip(".:-|\u2013\u2014 ").lower()
    return cleaned in _BLANK_MARKERS or cleaned == ""


def extract_urls(text: str) -> list[str]:
    """Find bare URLs, markdown links and angle-bracket-wrapped URLs, in order."""
    found: list[str] = []
    for match in re.finditer(r"\[[^\]]*\]\((?P<url>https?://[^)\s]+)\)", text):
        found.append(match.group("url"))
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if url not in found:
            found.append(url)
    return found


@dataclass(frozen=True, slots=True)
class Line:
    index: int
    indent: int
    text: str


@dataclass(frozen=True, slots=True)
class Item:
    """One numbered or bulleted entry, with its nesting path.

    Unnumbered `label: value` lines are also Items, with an empty path, so the binder
    can treat them uniformly.
    """

    path: tuple[int, ...]
    marker: str
    value: str
    start_line: int
    end_line: int
    indent: int

    @property
    def is_blank(self) -> bool:
        return is_blank_value(self.value)


@dataclass(frozen=True, slots=True)
class Tokenized:
    items: tuple[Item, ...]
    preamble: str
    lines: tuple[Line, ...]
    # Unnumbered `label: value` lines. Reports are sometimes written without any
    # numbering at all, and those must still bind by label.
    label_items: tuple[Item, ...] = ()

    def item_at(self, path: tuple[int, ...]) -> Item | None:
        for item in self.items:
            if item.path == path:
                return item
        return None

    def children_of(self, ordinal: int) -> tuple[Item, ...]:
        return tuple(i for i in self.items if len(i.path) > 1 and i.path[0] == ordinal)

    def candidates(self) -> tuple[Item, ...]:
        """Everything bindable, numbered items first."""
        return (*self.items, *self.label_items)


def _indent_of(raw: str) -> int:
    expanded = raw.replace("\t", "    ")
    return len(expanded) - len(expanded.lstrip(" "))


def tokenize(content: str) -> Tokenized:
    """Turn report text into a tree of numbered items."""
    lines: list[Line] = []
    for idx, raw in enumerate(
        normalize(content).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ):
        stripped = strip_emphasis(raw)
        lines.append(Line(index=idx, indent=_indent_of(raw), text=stripped.rstrip()))

    items: list[Item] = []
    label_items: list[Item] = []
    preamble_parts: list[str] = []

    # Track the item that a continuation line belongs to.
    current: Item | None = None
    current_ordinal_indent: int = -1

    def close(item: Item, end_line: int) -> None:
        items.append(
            Item(
                path=item.path,
                marker=item.marker,
                value=item.value.strip(),
                start_line=item.start_line,
                end_line=end_line,
                indent=item.indent,
            )
        )

    def next_bullet_index(base: tuple[int, ...]) -> int:
        """Bullets have no explicit number, so derive one from existing siblings.

        The in-progress `current` item is not yet in `items`, and consecutive bullets
        are exactly that case, so it must be counted too.
        """
        paths = [item.path for item in items]
        if current is not None:
            paths.append(current.path)
        siblings = [p[-1] for p in paths if len(p) == len(base) + 1 and p[:-1] == base]
        return (max(siblings) if siblings else 0) + 1

    for line in lines:
        if not line.text.strip():
            continue

        number_match = _NUMBER_RE.match(line.text.strip())
        bullet_match = None
        if not number_match:
            bullet_match = _BULLET_RE.match(line.text.strip())

        if number_match:
            raw_marker = number_match.group("marker")
            value = number_match.group("value")
            path = tuple(int(p) for p in raw_marker.split("."))
            if current is not None:
                close(current, line.index - 1)
                current = None
            current = Item(
                path=path,
                marker=raw_marker,
                value=value,
                start_line=line.index,
                end_line=line.index,
                indent=line.indent,
            )
            current_ordinal_indent = line.indent
            continue

        if bullet_match:
            value = bullet_match.group("value")
            # A bullet nests under the most recent top-level item.
            base: tuple[int, ...] = ()
            if current is not None:
                base = (current.path[0],)
            elif items:
                base = (items[-1].path[0],)
            if base:
                index = next_bullet_index(base)
                if current is not None:
                    close(current, line.index - 1)
                    current = None
                current = Item(
                    path=(*base, index),
                    marker=bullet_match.group("bullet"),
                    value=value,
                    start_line=line.index,
                    end_line=line.index,
                    indent=line.indent,
                )
                continue
            # Bullet with nothing to nest under: treat as prose.
            if current is not None:
                close(current, line.index - 1)
                current = None
            preamble_parts.append(line.text.strip())
            continue

        # Continuation of the current item, or preamble text.
        if current is not None and (
            line.indent > current_ordinal_indent or current.value.strip() == ""
        ):
            current = Item(
                path=current.path,
                marker=current.marker,
                value=f"{current.value} {line.text.strip()}".strip(),
                start_line=current.start_line,
                end_line=line.index,
                indent=current.indent,
            )
            continue

        if current is not None:
            close(current, line.index - 1)
            current = None

        # An unnumbered `label: value` line is bindable by label alone. Reports are
        # sometimes posted with no numbering whatsoever.
        stripped = line.text.strip()
        if _looks_like_label(stripped):
            label_items.append(
                Item(
                    path=(),
                    marker="",
                    value=stripped,
                    start_line=line.index,
                    end_line=line.index,
                    indent=line.indent,
                )
            )
            continue

        preamble_parts.append(stripped)

    if current is not None:
        close(current, lines[-1].index if lines else current.start_line)

    return Tokenized(
        items=tuple(items),
        preamble="\n".join(preamble_parts),
        lines=tuple(lines),
        label_items=tuple(label_items),
    )


_LABEL_LINE_RE: Final = re.compile(r"^[^:]{2,80}:.+$")


def _looks_like_label(text: str) -> bool:
    """Heuristic for `Название квалификации: INF`.

    Deliberately strict: a false positive steals a line from the preamble, so we
    require a colon, a short left side, and no sentence-ending punctuation.
    """
    if not _LABEL_LINE_RE.match(text):
        return False
    head = text.split(":", 1)[0]
    if head.endswith((".", "!", "?")):
        return False
    return len(head.split()) <= 8

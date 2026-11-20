"""Typed field value parsers.

Each parser turns raw report text into a structured value plus zero or more issues.
Parsers never raise: a bad value is an issue, not an exception, because a report that
fails validation must still be stored and shown to staff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from dsbot.modules.reports.text import extract_urls, is_blank_value, normalize, strip_emphasis

DISCORD_LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)
_IDN_SPLIT_RE = re.compile(
    r"^\s*(?P<formation>[A-Za-zА-Яа-я0-9+_.\-]{1,24})\s*[|/]\s*(?P<rest>.+?)\s*$"
)
_PAIR_RE = re.compile(r"^(?P<a>\d{1,2})\s*/\s*(?P<b>\d{2,})$")
_NUMBER_RE = re.compile(r"^(?P<n>\d{2,})$")

# A lore (RP) IDN is 4 digits, or 6 or more: three and five digit values are almost
# always a typo or a truncated paste, and quota counting depends on the identifier
# being right.
LORE_IDN_LENGTHS: Final = frozenset({4}) | frozenset(range(6, 32))

# The system (NRP) IDN is always exactly 4 digits. It is the server's own counter and
# never grows: a 6-digit number in that position is a lore IDN written in the wrong
# slot, not a valid system IDN.
SYSTEM_IDN_LENGTH: Final = 4


def _lore_idn_length_ok(value: str) -> bool:
    return len(value) in LORE_IDN_LENGTHS


def _system_idn_length_ok(value: str) -> bool:
    return len(value) == SYSTEM_IDN_LENGTH


def _plain_idn_ok(value: str | None) -> bool:
    """Shape 1: exactly 4 digits, nothing else."""
    return value is not None and len(value) == 4 and value.isdigit()


def _prefixed_idn_ok(value: str | None) -> bool:
    """Shape 2: the IDN after the slash is 00 followed by two digits."""
    return value is not None and len(value) == 4 and value.isdigit() and value.startswith("00")


@dataclass(slots=True)
class FieldIssue:
    code: str
    field_id: str | None
    severity: str = "error"  # error | warning
    detail: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "field": self.field_id,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass(slots=True)
class FieldResult:
    value: dict[str, Any] | None
    issues: list[FieldIssue] = field(default_factory=list)
    present: bool = False


def parse_formation_idn(
    raw: str, field_id: str, formations: list[str] | None = None
) -> FieldResult:
    """Parse `SOB | 01/0016` into formation plus IDN parts.

    Formations are a closed, finite set and are validated against it. The IDN that
    follows is *not* checked against any list: there are more than 10k characters and
    the numbering is open-ended.

    IDNs are never unique, so a collision is data, not an error. Parts are stored
    separately so a future roster can enforce rules without re-parsing history.
    """
    text = strip_emphasis(normalize(raw)).strip()
    if is_blank_value(text):
        return FieldResult(value=None, present=False)

    issues: list[FieldIssue] = []
    formation: str | None = None
    rest = text

    split = _IDN_SPLIT_RE.match(text)
    if split:
        formation = split.group("formation").upper()
        rest = split.group("rest").strip()
    else:
        issues.append(
            FieldIssue(
                code="idn_no_formation",
                field_id=field_id,
                severity="warning",
                detail={"raw": text},
            )
        )

    if formation is not None and formations:
        known = {f.upper() for f in formations}
        if formation not in known:
            issues.append(
                FieldIssue(
                    code="formation_unknown",
                    field_id=field_id,
                    detail={"raw": formation, "formations": sorted(known)},
                )
            )

    value: dict[str, Any] = {
        "formation": formation,
        "raw": text,
        "idn_text": rest,
        "idn_kind": "nrp",
        "sub_unit": None,
        "idn_number": None,
    }

    pair = _PAIR_RE.match(rest)
    if pair:
        number = pair.group("b")
        if not _lore_idn_length_ok(number):
            issues.append(
                FieldIssue(
                    code="idn_bad_length",
                    field_id=field_id,
                    detail={"raw": number, "expected": "4 digits, or 6+"},
                )
            )
        value["idn_kind"] = "rp_lore"
        value["sub_unit"] = pair.group("a").zfill(2)
        # Never pad: an IDN is reproduced exactly as written. "001" stays "001" and is
        # rejected as a bad length rather than silently becoming "0001".
        value["idn_number"] = number
        return FieldResult(value=value, issues=issues, present=True)

    number = _NUMBER_RE.match(rest)
    if number:
        digits = number.group("n")
        if not _lore_idn_length_ok(digits):
            issues.append(
                FieldIssue(
                    code="idn_bad_length",
                    field_id=field_id,
                    detail={"raw": digits, "expected": "4 digits, or 6+"},
                )
            )
        value["idn_number"] = digits
        return FieldResult(value=value, issues=issues, present=True)

    # Anything else is preserved verbatim so nothing is lost.
    issues.append(
        FieldIssue(
            code="idn_unparsed",
            field_id=field_id,
            detail={"raw": rest},
        )
    )
    return FieldResult(value=value, issues=issues, present=True)


def parse_enum(raw: str, field_id: str, options: list[str], *, strict: bool = True) -> FieldResult:
    """Match a qualification code, tolerating case, spacing and trailing commentary.

    Reporters write `t`, `D-U-C (Инструкторская система повышения)`, and `INF +`. All
    of those mean the code at the start, so the value is matched by its leading token
    rather than requiring the whole string to be exact.
    """
    text = strip_emphasis(normalize(raw)).strip(" .:-|")
    if is_blank_value(text):
        return FieldResult(value=None, present=False)

    if not options:
        upper = text.upper()
        return FieldResult(value={"code": upper, "raw": text, "matched": False}, present=True)

    def compact(value: str) -> str:
        return re.sub(r"[\s\-_]", "", value.upper())

    candidates = [text]
    head = re.split(r"[(\[]", text, maxsplit=1)[0].strip()
    if head and head != text:
        candidates.append(head)
    first_token = re.split(r"[\s,;]+", text, maxsplit=1)[0].strip()
    if first_token and first_token not in candidates:
        candidates.append(first_token)

    for candidate in candidates:
        for option in options:
            if candidate.upper() == option.upper():
                return FieldResult(
                    value={"code": option, "raw": text, "matched": True}, present=True
                )
        compacted = compact(candidate)
        for option in options:
            if compacted == compact(option):
                return FieldResult(
                    value={"code": option, "raw": text, "matched": True}, present=True
                )

    return FieldResult(
        value={"code": candidates[0].upper(), "raw": text, "matched": False},
        issues=[
            FieldIssue(
                code="enum_unknown_option",
                field_id=field_id,
                # An unrecognised qualification cannot be counted, so by default this
                # is an error. Schemas that expect free text can opt out.
                severity="error" if strict else "warning",
                detail={"raw": text, "options": options},
            ),
            # Always show the valid list: "pepe" alone tells the reporter nothing.
            FieldIssue(
                code="list_allowed_qualifications",
                field_id=field_id,
                detail={"options": options},
            ),
        ],
        present=True,
    )


def parse_discord_link(raw: str, field_id: str) -> FieldResult:
    text = normalize(raw)
    if is_blank_value(text) or not text.strip():
        return FieldResult(value=None, present=False)

    urls = extract_urls(text)
    for url in urls:
        match = DISCORD_LINK_RE.search(url)
        if match:
            return FieldResult(
                value={
                    "url": url,
                    "guild_id": int(match.group("guild")),
                    "channel_id": int(match.group("channel")),
                    "message_id": int(match.group("message")),
                },
                present=True,
            )

    if not urls:
        return FieldResult(
            value={"raw": strip_emphasis(text)},
            issues=[FieldIssue(code="link_not_a_url", field_id=field_id, detail={"raw": text})],
            present=True,
        )

    return FieldResult(
        value={"url": urls[0], "raw": text},
        issues=[FieldIssue(code="link_not_discord", field_id=field_id, detail={"url": urls[0]})],
        present=True,
    )


def parse_attachment(raw: str, field_id: str, attachment_count: int, slot: int = 0) -> FieldResult:
    """Evidence fields: the value is an attachment, not text.

    Handles "3. <photo>" (attachment in the same message), "3." with nothing
    (attachment in a follow-up message from the same author), and junk leftovers.

    Each evidence field claims exactly one attachment slot, so one photo cannot
    satisfy two separate evidence requirements.
    """
    text = strip_emphasis(normalize(raw)).strip()
    has_text = not is_blank_value(text)
    if attachment_count > slot:
        return FieldResult(
            value={
                "attachment_count": attachment_count,
                "slot": slot,
                "raw": text if has_text else None,
            },
            present=True,
        )
    if has_text:
        return FieldResult(
            value={"raw": text, "attachment_count": attachment_count, "slot": slot},
            issues=[
                FieldIssue(
                    code="evidence_text_without_file",
                    field_id=field_id,
                    severity="warning",
                    detail={"raw": text},
                )
            ],
            present=True,
        )
    return FieldResult(value=None, present=False)


def parse_count_fraction(raw: str, field_id: str) -> FieldResult:
    """Parse `7/8`, `7 of 8`, `8/8` used in training result reports."""
    text = strip_emphasis(normalize(raw)).strip()
    if is_blank_value(text):
        return FieldResult(value=None, present=False)
    match = re.search(r"(\d{1,3})\s*[/\\]\s*(\d{1,3})", text)
    if match:
        got, total = int(match.group(1)), int(match.group(2))
        issues: list[FieldIssue] = []
        if total == 0 or got > total:
            issues.append(
                FieldIssue(
                    code="fraction_invalid",
                    field_id=field_id,
                    detail={"got": got, "total": total},
                )
            )
        return FieldResult(
            value={"got": got, "total": total, "raw": text},
            issues=issues,
            present=True,
        )
    return FieldResult(
        value={"raw": text},
        issues=[FieldIssue(code="fraction_unparsed", field_id=field_id, detail={"raw": text})],
        present=True,
    )


def parse_name_ref(raw: str, field_id: str) -> FieldResult:
    """A person reference. Resolution to a Discord user is out of scope here."""
    text = strip_emphasis(normalize(raw)).strip()
    if is_blank_value(text):
        return FieldResult(value=None, present=False)
    return FieldResult(value={"raw": text}, present=True)


def parse_identity(
    raw: str,
    field_id: str,
    formations: list[str] | None = None,
    lore_prefixes: list[str] | None = None,
    lore_prefix_formations: list[str] | None = None,
) -> FieldResult:
    """Parse the identity field: formation plus IDN(s).

    Two IDN shapes exist:

        1) plain, 4 digits                      SOB | 0016   ARC | 0042
        2) prefixed, PP/00NN                    SOB | 01/0016   SOB | 00/0087
           PP must be an allowed prefix; the two digits after the slash are 00.

    Real reports also carry a SECOND 4-digit IDN, brackets, labels, a backslash instead
    of a slash, and sometimes several soldiers. All of that is accepted, because these
    are correct reports and rejecting them would be wrong:

        SOB | 01/0038 [8289]        prefixed plus system IDN in brackets
        SOB | 01\\0095 [8076]        backslash instead of slash
        SOB | 1262 | 4313           plain plus a second IDN
        SOB | NRP-4113 | RP-1262 | RC-62
        ARC | (8377) 01\\0016        numbers in either order
        ARC | 7784 ; ARC | 8440     several soldiers in one report

    Only the first identity is the report's subject; the rest are kept in
    `all_identities` so nothing is lost.

    Formations compare case-insensitively, with Cyrillic look-alikes folded to Latin,
    because reporters type `СT` and `CT` and mean the same thing.

    Prefixes are formation-specific: `01/` and `00/` are SOB squads, valid for SOB only.
    """
    text = strip_emphasis(normalize(raw)).strip()
    if is_blank_value(text):
        return FieldResult(value=None, present=False)

    known = {f.upper() for f in (formations or [])}
    allowed_prefixes = [p.zfill(2) for p in (lore_prefixes or ["01", "00"])]
    prefix_formations = {f.upper() for f in (lore_prefix_formations or ["SOB"])}
    issues: list[FieldIssue] = []

    all_identities: list[dict[str, Any]] = []
    formation_reported = False
    prefix_formation_reported = False
    first: dict[str, Any] | None = None

    for chunk in _split_identities(text):
        value, chunk_issues = _parse_one_identity(
            chunk,
            known=known,
            allowed_prefixes=allowed_prefixes,
            prefix_formations=prefix_formations,
            field_id=field_id,
        )
        all_identities.append(value)
        for issue in chunk_issues:
            # Formation and prefix problems are properties of the report, not of each
            # soldier, so report them once.
            if issue.code == "formation_unknown":
                if formation_reported:
                    continue
                formation_reported = True
            if issue.code == "lore_prefix_not_allowed_for_formation":
                if prefix_formation_reported:
                    continue
                prefix_formation_reported = True
            issues.append(issue)
        if first is None:
            first = value

    if first is None:
        return FieldResult(value=None, issues=issues, present=False)

    if formation_reported:
        issues.append(
            FieldIssue(
                code="list_allowed_formations",
                field_id=field_id,
                detail={"formations": sorted(known)},
            )
        )

    value = dict(first)
    value["raw"] = text
    value["all_identities"] = all_identities
    value["extra_identities"] = len(all_identities) > 1
    return FieldResult(
        value=value,
        issues=issues,
        present=bool(first.get("formation") or first.get("lore_idn")),
    )


def _split_identities(text: str) -> list[str]:
    """Split one field that may describe several soldiers.

    `;` separates soldiers. `,` is not used: it appears inside qualification names.
    """
    parts = [p.strip() for p in text.split(";") if p.strip()]
    return parts or [text]


_CYRILLIC_FOLD = str.maketrans(
    {"С": "C", "Т": "T", "А": "A", "Е": "E", "О": "O", "Р": "P", "Х": "X", "М": "M", "К": "K"}
)


def normalise_formation(token: str) -> str:
    """Upper-case a formation and fold Cyrillic look-alikes to Latin.

    `СT` (Cyrillic es) and `CT` are the same formation to a human. Treating them as
    different rejected real reports.
    """
    return token.upper().translate(_CYRILLIC_FOLD)


def _parse_one_identity(
    chunk: str,
    *,
    known: set[str],
    allowed_prefixes: list[str],
    prefix_formations: set[str],
    field_id: str,
) -> tuple[dict[str, Any], list[FieldIssue]]:
    """Parse a single soldier's identity expression."""
    issues: list[FieldIssue] = []
    stripped = chunk.strip()

    # Brackets, parens and labels are decoration; the numbers and the formation matter.
    cleaned = stripped.replace("[", " ").replace("]", " ").replace("(", " ").replace(")", " ")

    formation: str | None = None
    numbers: list[str] = []
    has_pipe = "|" in stripped

    # Brackets are NOT allowed: the template uses `|`, and accepting `[8076]` silently
    # teaches the wrong format. Report it instead of quietly parsing through it.
    if "[" in stripped or "]" in stripped:
        issues.append(
            FieldIssue(
                code="brackets_not_allowed",
                field_id=field_id,
                detail={"raw": stripped},
            )
        )

    # Split the formation slot from the IDN slot. A pipe is authoritative; without one,
    # the first word is the formation and the rest are numbers.
    left, _, right = stripped.partition("|")
    if not has_pipe:
        pieces = re.split(r"[|/\\,:\s]+", cleaned)
        pieces = [p for p in pieces if p.strip()]
        left = pieces[0] if pieces else ""
        right = " ".join(pieces[1:]) if len(pieces) > 1 else ""

    # The formation is the first word on the left, so "ARC Alpha" is ARC plus noise.
    formation_token = left.strip().split()[0] if left.strip() else ""
    if formation_token:
        formation = normalise_formation(formation_token.strip(" .,;:-"))

    # Numbers live on the right side of the pipe, plus anything numeric on the left
    # (`ARC 0085`). Brackets and labels are decoration.
    numbers_text = re.sub(r"[\[\]()]", " ", f"{left} {right}")
    for raw_token in re.split(r"[|/\\,:\s]+", numbers_text):
        token = raw_token.strip(" .,;:-")
        if not token:
            continue
        labelled = re.fullmatch(r"(?i)(nrp|rp|rc|idn)[\s\-_]*(\d{2,})", token)
        if labelled:
            numbers.append(labelled.group(2))
            continue
        digits_only = re.sub(r"\D", "", token)
        if digits_only and digits_only == token:
            numbers.append(digits_only)

    # The formation word may itself be numeric ("99 | 9902"). It is the formation, not
    # an IDN, so drop one occurrence of it from the numbers.
    if formation is not None and numbers and numbers[0] == formation:
        numbers = numbers[1:]

    # Formations are compared case-insensitively: "501st" and "501ST" are the same.
    if formation is not None:
        for candidate in known:
            if candidate.upper() == formation.upper():
                formation = candidate
                break

    if formation is None:
        issues.append(
            FieldIssue(
                code="idn_no_formation",
                field_id=field_id,
                severity="warning",
                detail={"raw": stripped},
            )
        )
    elif known and formation.upper() not in {k.upper() for k in known}:
        issues.append(
            FieldIssue(
                code="formation_unknown",
                field_id=field_id,
                detail={"raw": formation, "formations": sorted(known)},
            )
        )

    prefix: str | None = None
    lore_idn: str | None = None
    nrp_idn: str | None = None

    # Only a genuine `PP/NN` pair is a prefix. A short number after a pipe is a separate
    # IDN, not a prefix: `1262 | 4313` has two plain IDNs.
    prefix = _detect_prefix(stripped)
    if prefix is not None:
        pair = _PREFIX_PAIR.search(stripped)
        if pair is not None:
            lore_idn = pair.group(2)
        # Whatever remains after removing the prefix and its body is a separate IDN
        # (`[8076]`), never a second prefix.
        remaining = [n for n in numbers if n != lore_idn and n.zfill(2) != prefix]
        nrp_idn = remaining[0] if remaining else None
    elif numbers:
        lore_idn = numbers[0]
        nrp_idn = numbers[1] if len(numbers) > 1 else None

    if not numbers:
        issues.append(
            FieldIssue(
                code="idn_missing",
                field_id=field_id,
                detail={"raw": stripped},
            )
        )

    if prefix is None:
        if lore_idn is not None and not _plain_idn_ok(lore_idn):
            issues.append(
                FieldIssue(
                    code="idn_bad_shape",
                    field_id=field_id,
                    detail={"raw": lore_idn, "expected": "4 digits (plain IDN)"},
                )
            )
    else:
        if formation is not None and formation.upper() not in {
            f.upper() for f in prefix_formations
        }:
            issues.append(
                FieldIssue(
                    code="lore_prefix_not_allowed_for_formation",
                    field_id=field_id,
                    detail={
                        "formation": formation,
                        "allowed_formations": sorted(prefix_formations),
                    },
                )
            )
        if prefix not in allowed_prefixes:
            issues.append(
                FieldIssue(
                    code="lore_prefix_invalid",
                    field_id=field_id,
                    detail={"raw": prefix, "allowed": allowed_prefixes},
                )
            )
        if not _prefixed_idn_ok(lore_idn):
            issues.append(
                FieldIssue(
                    code="idn_bad_shape",
                    field_id=field_id,
                    detail={
                        "raw": f"{prefix}/{lore_idn}",
                        "expected": "6 digits as PP/00NN (e.g. 01/0016)",
                    },
                )
            )

    if nrp_idn is not None and not _system_idn_length_ok(nrp_idn):
        issues.append(
            FieldIssue(
                code="nrp_bad_length",
                field_id=field_id,
                detail={
                    "raw": nrp_idn,
                    "kind": "system IDN",
                    "expected": "exactly 4 digits",
                },
            )
        )

    value: dict[str, Any] = {
        "formation": formation,
        "raw": stripped,
        "idn_text": "/".join(numbers) if numbers else None,
        "lore_idn": lore_idn,
        "nrp_idn": nrp_idn,
        "prefix": prefix,
        "idn_kind": "plain" if prefix is None else "prefixed",
        "idn_number": lore_idn,
        "canonical": (f"{formation}|{prefix}/{lore_idn}" if prefix else f"{formation}|{lore_idn}"),
        "has_pipe": has_pipe,
    }
    return value, issues


_PREFIX_PAIR = re.compile(r"(?<!\d)(\d{1,2})\s*[/\\]\s*(\d{2,})")


def _detect_prefix(text: str) -> str | None:
    """Return the prefix only when the text really contains a `PP/NN` pair."""
    match = _PREFIX_PAIR.search(text)
    if match is None:
        return None
    return match.group(1).zfill(2)


def parse_text(raw: str) -> FieldResult:
    text = strip_emphasis(normalize(raw)).strip()
    if is_blank_value(text):
        return FieldResult(value=None, present=False)
    return FieldResult(value={"raw": text}, present=True)

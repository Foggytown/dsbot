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
    """Match a qualification code, tolerating case and surrounding punctuation."""
    text = strip_emphasis(normalize(raw)).strip(" .:-|")
    if is_blank_value(text):
        return FieldResult(value=None, present=False)

    upper = text.upper()
    if not options:
        return FieldResult(value={"code": upper, "raw": text, "matched": False}, present=True)

    for option in options:
        if upper == option.upper():
            return FieldResult(value={"code": option, "raw": text, "matched": True}, present=True)

    # Tolerate '+', spaces and separators: "INF +" and "inf+" are the same thing.
    compact = re.sub(r"[\s\-_]", "", upper)
    for option in options:
        if compact == re.sub(r"[\s\-_]", "", option.upper()):
            return FieldResult(value={"code": option, "raw": text, "matched": True}, present=True)

    return FieldResult(
        value={"code": upper, "raw": text, "matched": False},
        issues=[
            FieldIssue(
                code="enum_unknown_option",
                field_id=field_id,
                # An unrecognised qualification cannot be counted, so by default this
                # is an error. Schemas that expect free text can opt out.
                severity="error" if strict else "warning",
                detail={"raw": text, "options": options},
            )
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
) -> FieldResult:
    """Parse the identity field: formation plus lore IDN and/or NRP IDN.

    Accepts the shapes people actually write:
        SOB | 0016
        SOB | 01/0016
        SOB | 01/0016 | 0017

    Only two vocabularies exist here:

      * the formation - a closed list: SOB, ARC, CT, ...
      * the IDN      - open-ended; only its length is checked (4 digits, or 6+)

    Squads (ALPHA, NULL) are deliberately NOT modelled: reporters write their
    formation, and a squad is internal structure of that formation. `01/0016` is
    accepted as a normal lore IDN with a numeric prefix, whatever caused that prefix.
    """
    text = strip_emphasis(normalize(raw)).strip()
    if is_blank_value(text):
        return FieldResult(value=None, present=False)

    known = {f.upper() for f in (formations or [])}
    issues: list[FieldIssue] = []

    parts = [p.strip() for p in re.split(r"[|/\\]", text) if p.strip()]
    formation: str | None = None
    numbers: list[str] = []

    for part in parts:
        cleaned = part.strip(" .,:;")
        upper = cleaned.upper()
        if formation is None and upper in known:
            formation = upper
            continue
        if formation is None:
            token = re.split(r"\s+", cleaned)[0].upper() if cleaned else ""
            if token and not token.isdigit():
                formation = token
                continue
        if re.fullmatch(r"\d{1,8}", cleaned):
            numbers.append(cleaned)

    if formation is None:
        issues.append(
            FieldIssue(
                code="idn_no_formation",
                field_id=field_id,
                severity="warning",
                detail={"raw": text},
            )
        )
    elif known and formation not in known:
        issues.append(
            FieldIssue(
                code="formation_unknown",
                field_id=field_id,
                detail={"raw": formation, "formations": sorted(known)},
            )
        )

    lore_idn: str | None = None
    nrp_idn: str | None = None
    prefix: str | None = None

    # A numeric group of length 1-2 before a 4-digit number is a lore prefix, e.g. 01/0016.
    if len(numbers) >= 2 and len(numbers[0]) <= 2:
        prefix = numbers[0].zfill(2)
        lore_idn = numbers[1]
        nrp_idn = numbers[2] if len(numbers) > 2 else None
    elif numbers:
        lore_idn = numbers[0]
        nrp_idn = numbers[1] if len(numbers) > 1 else None

    for idn_label, idn_value in (("lore IDN", lore_idn), ("NRP IDN", nrp_idn)):
        if idn_value is None:
            continue
        # The system (NRP) IDN is always 4 digits; a lore IDN may be 4 or 6+.
        # Reporting the wrong rule is worse than no check, so the messages differ.
        if idn_label == "NRP IDN":
            ok = _system_idn_length_ok(idn_value)
            expected = "exactly 4 digits (system IDN)"
        else:
            ok = _lore_idn_length_ok(idn_value)
            expected = "4 digits, or 6+"
        if not ok:
            issues.append(
                FieldIssue(
                    code="nrp_bad_length" if idn_label == "NRP IDN" else "idn_bad_length",
                    field_id=field_id,
                    detail={
                        "raw": idn_value,
                        "kind": idn_label,
                        "expected": expected,
                    },
                )
            )

    value: dict[str, Any] = {
        "formation": formation,
        "raw": text,
        "idn_text": "/".join(numbers) if numbers else None,
        "lore_idn": lore_idn,
        "nrp_idn": nrp_idn,
        "prefix": prefix,
        "idn_kind": "rp_lore" if prefix is not None else "nrp",
        "idn_number": lore_idn,
    }
    return FieldResult(value=value, issues=issues, present=True)


def parse_text(raw: str) -> FieldResult:
    text = strip_emphasis(normalize(raw)).strip()
    if is_blank_value(text):
        return FieldResult(value=None, present=False)
    return FieldResult(value={"raw": text}, present=True)

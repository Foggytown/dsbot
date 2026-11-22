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
    r"^\s*(?P<formation>[A-Za-z\u0410-\u042f\u0430-\u044f0-9+_.\-]{1,24})\s*[|/]\s*(?P<rest>.+?)\s*$"
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

    Reporters write lowercase codes, add explanations after the code, and sometimes
    write `INF +`. All of those mean the code at the start, so the value is matched by
    its leading token rather than requiring the whole string to be exact.
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
                    # An error, not a warning. "Photo" written with no file attached is
                    # the absence of evidence, and evidence is what the report is for.
                    # As a warning it was reported and then ignored, so a report with
                    # "3. фото" and zero attachments validated as valid.
                    severity="error",
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


_CYRILLIC_FOLD = str.maketrans(
    {
        "\u0421": "C",
        "\u0422": "T",
        "\u0410": "A",
        "\u0415": "E",
        "\u041e": "O",
        "\u0420": "P",
        "\u0425": "X",
        "\u041c": "M",
        "\u041a": "K",
    }
)


def parse_identity(
    raw: str,
    field_id: str,
    formations: list[str] | None = None,
    lore_prefixes: list[str] | None = None,
    lore_prefix_formations: list[str] | None = None,
) -> FieldResult:
    """Parse the identity field. STRICT about shape, per soldier.

    The field may list several soldiers, separated by `,`. Each one is parsed and checked
    independently, and the first is the report's subject.

    Each soldier must be written exactly as the template says:

        FORMATION | IDN                    ARC | 0042
        FORMATION | PP/00NN | NRNN         SOB | 01/0016 | 0017

    Where:
        * FORMATION is one of the configured formations, case-insensitive
        * IDN and NRNN are exactly 4 digits
        * PP is 01 or 00, allowed only for the configured formations (SOB)
        * the two digits after the slash are always 00

    Everything else is an error naming the specific problem, because these values feed
    weekly reports and silent tolerance would produce wrong statistics.

    Rejected, with the reason reported to the reporter:
        ARC | 01/0016            prefix belongs to SOB
        SOB | 01/0038            prefixed IDN needs its system IDN
        SOB | 01\\0016            backslash instead of slash
        SOB | 01/0038 [8289]     brackets
        ARC Alpha | 01/0016      extra word
        212th | Revers           name instead of a number
        99 | 9902                unknown formation
        sob | 0016               wrong capitalisation

    Accepted: either IDN form, optionally followed by a 4-digit system IDN -
    `SOB | 0016 | 0017` and `SOB | 01/0016 | 0017` both name two IDs.
    """
    text = strip_emphasis(normalize(raw)).strip()
    if is_blank_value(text):
        return FieldResult(value=None, present=False)

    # Formation matching is EXACT: the template spelling is the specification. Keep the
    # configured values as-is so `GUARD` does not match `Guard`.
    canonical = {f: f for f in (formations or [])}
    known = set(canonical)
    prefix_formations = {f.upper() for f in (lore_prefix_formations or ["SOB"])}
    allowed_prefixes = [p.zfill(2) for p in (lore_prefixes or ["01", "00"])]

    # Structural problems that apply to the whole field.
    field_issues: list[FieldIssue] = []
    if "[" in text or "]" in text:
        field_issues.append(
            FieldIssue(code="brackets_not_allowed", field_id=field_id, detail={"raw": text})
        )
    if "\\" in text:
        field_issues.append(
            FieldIssue(code="backslash_not_allowed", field_id=field_id, detail={"raw": text})
        )

    # Semicolons are a very common way to separate soldiers, but the template uses a
    # comma. Say so explicitly instead of failing later with a confusing IDN error.
    if ";" in text:
        field_issues.append(
            FieldIssue(code="semicolon_not_allowed", field_id=field_id, detail={"raw": text})
        )
        return FieldResult(
            value={
                "formation": None,
                "raw": text,
                "idn_text": None,
                "lore_idn": None,
                "nrp_idn": None,
                "prefix": None,
                "idn_kind": "plain",
                "idn_number": None,
                "canonical": None,
                "all_identities": [],
                "extra_identities": False,
            },
            issues=field_issues,
            present=True,
        )

    soldiers = [part.strip() for part in text.split(",")]
    soldiers = [soldier for soldier in soldiers if soldier]

    all_identities: list[dict[str, Any]] = []
    first_value: dict[str, Any] | None = None
    issues = field_issues
    formation_reported = False

    for soldier in soldiers:
        soldier_value, soldier_issues = _parse_one_soldier(
            soldier,
            known=known,
            canonical=canonical,
            allowed_prefixes=allowed_prefixes,
            prefix_formations=prefix_formations,
            field_id=field_id,
        )
        all_identities.append(soldier_value)
        for issue in soldier_issues:
            # Formation problems are properties of the field, so report the list once.
            if issue.code == "formation_unknown":
                if formation_reported:
                    issues = [
                        i
                        for i in issues
                        if i.code not in ("formation_unknown", "list_allowed_formations")
                    ]
                else:
                    formation_reported = True
            issues.append(issue)
        if first_value is None:
            first_value = soldier_value

    if first_value is None:
        return FieldResult(value=None, issues=issues, present=False)

    value = dict(first_value)
    value["raw"] = text
    value["all_identities"] = all_identities
    value["extra_identities"] = len(all_identities) > 1
    if len(all_identities) > 1:
        value["soldier_count"] = len(all_identities)
    # The reporter did write something in this field, so it is present even when it is
    # malformed. Reporting it as "missing" as well would be a second, misleading error.
    return FieldResult(value=value, issues=issues, present=True)


def _parse_one_soldier(
    soldier: str,
    *,
    known: set[str],
    canonical: dict[str, str],
    allowed_prefixes: list[str],
    prefix_formations: set[str],
    field_id: str,
) -> tuple[dict[str, Any], list[FieldIssue]]:
    """Parse exactly one soldier, written `FORMATION | IDN [| NRNN]`."""
    issues: list[FieldIssue] = []
    value: dict[str, Any] = {
        "formation": None,
        "raw": soldier,
        "idn_text": None,
        "lore_idn": None,
        "nrp_idn": None,
        "prefix": None,
        "idn_kind": "plain",
        "idn_number": None,
        "canonical": None,
    }

    parts = [part.strip() for part in soldier.split("|")]
    parts = [part for part in parts if part]

    if len(parts) < 2:
        issues.append(
            FieldIssue(code="identity_bad_format", field_id=field_id, detail={"raw": soldier})
        )
        return value, issues

    # --- formation ------------------------------------------------------------
    # Exact match only. The template is the specification: `Guard`, `Jedi`, `212th`,
    # `501st`. All-caps or any other capitalisation is a mistake to be corrected, not a
    # variant to be folded, because these values are grouped in weekly reports.
    formation_raw = parts[0]
    if len(formation_raw.split()) > 1:
        issues.append(
            FieldIssue(
                code="formation_not_single_word",
                field_id=field_id,
                detail={"raw": formation_raw},
            )
        )
    formation = formation_raw.strip()
    if not formation:
        issues.append(
            FieldIssue(code="idn_no_formation", field_id=field_id, detail={"raw": soldier})
        )
        return value, issues

    value["formation"] = formation
    if known and formation not in known:
        issues.append(
            FieldIssue(
                code="formation_unknown",
                field_id=field_id,
                detail={"raw": formation, "formations": sorted(known)},
            )
        )
        issues.append(
            FieldIssue(
                code="list_allowed_formations",
                field_id=field_id,
                detail={"formations": sorted(known)},
            )
        )
    else:
        value["formation"] = canonical.get(formation, formation)

    # --- IDN(s) ---------------------------------------------------------------
    idn_part = parts[1]
    system_part = parts[2] if len(parts) > 2 else None

    if re.match(r"(?i)(nrp|rp|rc|idn)[\s\-_]", idn_part):
        issues.append(
            FieldIssue(code="idn_labelled_not_allowed", field_id=field_id, detail={"raw": idn_part})
        )
        return value, issues

    if len(parts) > 3:
        issues.append(
            FieldIssue(code="identity_bad_format", field_id=field_id, detail={"raw": soldier})
        )

    prefix: str | None = None
    lore_idn: str | None = None

    if "/" in idn_part:
        prefix_raw, _, body = idn_part.partition("/")
        prefix_text = prefix_raw.strip()
        prefix = prefix_text.zfill(2) if prefix_text.isdigit() and len(prefix_text) <= 2 else None
        lore_idn = body.strip()

        if prefix is None:
            issues.append(
                FieldIssue(code="identity_bad_format", field_id=field_id, detail={"raw": soldier})
            )
            return value, issues

        if formation.upper() not in {f.upper() for f in prefix_formations}:
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
                        "expected": "PP/00NN, for example 01/0016",
                    },
                )
            )
        if system_part is None:
            issues.append(
                FieldIssue(
                    code="nrp_missing",
                    field_id=field_id,
                    detail={"raw": soldier, "prefix": prefix, "lore_idn": lore_idn},
                )
            )
        elif not _system_idn_length_ok(system_part):
            issues.append(
                FieldIssue(
                    code="nrp_bad_length",
                    field_id=field_id,
                    detail={"raw": system_part, "expected": "exactly 4 digits"},
                )
            )
        else:
            value["nrp_idn"] = system_part
    else:
        lore_idn = idn_part
        if not _plain_idn_ok(lore_idn):
            issues.append(
                FieldIssue(
                    code="idn_bad_shape",
                    field_id=field_id,
                    detail={"raw": lore_idn, "expected": "exactly 4 digits"},
                )
            )
        if system_part is not None:
            # A second number after a plain IDN is legitimate: some soldiers have both
            # a lore IDN and a system (NRP) IDN, and both are 4 digits. Accept it and
            # keep it, so nothing is lost.
            if not _system_idn_length_ok(system_part):
                issues.append(
                    FieldIssue(
                        code="nrp_bad_length",
                        field_id=field_id,
                        detail={"raw": system_part, "expected": "exactly 4 digits"},
                    )
                )
            else:
                value["nrp_idn"] = system_part

    value.update(
        {
            "prefix": prefix,
            "lore_idn": lore_idn,
            "idn_text": idn_part,
            "idn_kind": "plain" if prefix is None else "prefixed",
            "idn_number": lore_idn,
            "canonical": (
                f"{formation}|{prefix}/{lore_idn}" if prefix else f"{formation}|{lore_idn}"
            ),
        }
    )
    return value, issues


def parse_text(raw: str) -> FieldResult:
    text = strip_emphasis(normalize(raw)).strip()
    if is_blank_value(text):
        return FieldResult(value=None, present=False)
    return FieldResult(value={"raw": text}, present=True)

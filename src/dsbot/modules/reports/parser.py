"""Schema selection, field binding, and validation.

Pipeline for one message group:
  1. score every schema bound to the channel
  2. bind tokenised items to schema fields (label match, then ordinal)
  3. parse each bound value with its typed parser
  4. check required-ness, including cross-field conditions
  5. attach evidence from follow-up messages by the same author

Every step is recorded in `parsing` so a staff member can see *why* a report was
judged malformed rather than just that it was.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dsbot.modules.reports.fields import (
    FieldIssue,
    FieldResult,
    parse_attachment,
    parse_count_fraction,
    parse_discord_link,
    parse_enum,
    parse_formation_idn,
    parse_identity,
    parse_name_ref,
    parse_text,
)
from dsbot.modules.reports.schema import FieldSpec, FormSchemaDef, Rule
from dsbot.modules.reports.text import (
    Item,
    Tokenized,
    is_blank_value,
    normalize_label,
    tokenize,
)

FUZZY_THRESHOLD = 0.72


@dataclass(slots=True)
class GroupMessage:
    """One message belonging to a possible report.

    A report is not necessarily a single message: the evidence photos may arrive in a
    follow-up by the same author, which is called out in the live template.
    """

    message_id: int
    author_id: int
    created_at: datetime
    content: str
    attachment_count: int = 0
    urls: tuple[str, ...] = ()
    # Server nickname (display name), used when addressing the author in a warning.
    author_name: str | None = None


@dataclass(slots=True)
class ReportCandidate:
    """A group of messages that may contain one report."""

    messages: list[GroupMessage]

    @property
    def primary(self) -> GroupMessage:
        return self.messages[0]

    @property
    def total_attachments(self) -> int:
        return sum(m.attachment_count for m in self.messages)


@dataclass(slots=True)
class BoundField:
    spec: FieldSpec
    item_path: tuple[int, ...] | None
    label_text: str | None
    resolution: str  # label | ordinal | absent
    raw_value: str
    result: FieldResult


@dataclass(slots=True)
class ParseOutput:
    matched: bool
    schema_key: str | None
    score: float
    fields: list[BoundField] = field(default_factory=list)
    issues: list[FieldIssue] = field(default_factory=list)
    attribution_raw: str | None = None
    attribution_field: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[FieldIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[FieldIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def is_valid(self) -> bool:
        return self.matched and not self.errors

    def field_value(self, field_id: str) -> dict[str, Any] | None:
        for bound in self.fields:
            if bound.spec.id == field_id:
                return bound.result.value
        return None


# --- schema selection ------------------------------------------------------


def score_schema(schema: FormSchemaDef, tokenized: Tokenized, content: str) -> float:
    """Score how well a schema matches a message.

    Labels and anchors both count. Template markers disqualify: a message carrying
    them is the template itself, not a report.

    Partial credit matters here. A report that is missing numbered item 4, or that
    dropped the labels and kept only the numbers, is a *malformed report* and must be
    warned about - not silently ignored. Only text that has no form-like structure at
    all should score zero.
    """
    haystack = normalize_label(content)
    if any(normalize_label(marker) in haystack for marker in schema.template_markers):
        return 0.0

    if not tokenized.items and not tokenized.label_items:
        # No numbered structure and no labels: only prose.
        return 0.0

    if not tokenized.items:
        # Label-only form: score purely on label coverage.
        label_hits = sum(1 for spec in schema.fields if _match_label(spec, tokenized) is not None)
        if schema.fields:
            return round(2.0 * (label_hits / len(schema.fields)), 4)
        return 0.0

    # A numbered form of any shape scores above zero; a malformed report must be
    # warned about rather than silently ignored.
    score = 0.5 if tokenized.items else 0.0

    matched_labels = sum(1 for spec in schema.fields if _match_label(spec, tokenized) is not None)
    if schema.fields:
        score += 2.0 * (matched_labels / len(schema.fields))

    spec_ordinals = {s.ordinal for s in schema.fields if s.ordinal}
    if spec_ordinals:
        present = {item.path[0] for item in tokenized.items if len(item.path) == 1}
        score += len(present & spec_ordinals) / len(spec_ordinals)

    for anchor in schema.anchors:
        if normalize_label(anchor) in haystack:
            score += 0.5

    return round(score, 4)


def select_schema(
    schemas: list[FormSchemaDef], content: str
) -> tuple[FormSchemaDef | None, float, dict[str, float]]:
    tokenized = tokenize(content)
    scores = {s.key: score_schema(s, tokenized, content) for s in schemas}
    if not scores:
        return None, 0.0, {}
    best_key = max(scores, key=lambda k: scores[k])
    best = next(s for s in schemas if s.key == best_key)
    if scores[best_key] < best.min_score:
        return None, scores[best_key], scores
    return best, scores[best_key], scores


# --- field binding ---------------------------------------------------------


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _match_label(spec: FieldSpec, tokenized: Tokenized) -> Item | None:
    """Find the item whose inline label names this field."""
    if not spec.labels:
        return None
    wanted = [normalize_label(label) for label in spec.labels]
    best: tuple[float, Item] | None = None
    for item in tokenized.candidates():
        inline_label, _separator, _rest = _split_inline_label(item.value)
        if inline_label is None:
            continue
        candidate = normalize_label(inline_label)
        if not candidate:
            continue
        for target in wanted:
            if candidate == target:
                return item
            if target in candidate or candidate in target:
                ratio = 0.95
            else:
                ratio = _similar(candidate, target)
            if ratio >= FUZZY_THRESHOLD and (best is None or ratio > best[0]):
                best = (ratio, item)
    return best[1] if best else None


def _split_inline_label(value: str) -> tuple[str | None, str | None, str]:
    """Split `Название квалификации: INF` into label and rest.

    Only splits when the left side looks like a label (short, no sentence
    punctuation), so a value that happens to contain a colon is not mangled.
    """
    for separator in (":", " - ", " — ", " – "):
        index = value.find(separator)
        if index <= 0:
            continue
        label = value[:index].strip()
        if len(label) > 80 or label.endswith((".", "!", "?")):
            continue
        if len(label.split()) > 10:
            continue
        return label, separator, value[index + len(separator) :].strip()
    return None, None, value


def bind_fields(
    schema: FormSchemaDef, tokenized: Tokenized, attachment_count: int
) -> list[BoundField]:
    """Resolve each schema field to an item, then parse its value."""
    bound: list[BoundField] = []
    used_paths: set[tuple[int, ...]] = set()

    # Pass 1: explicit labels win over position.
    by_label: dict[str, Item] = {}
    for spec in schema.fields:
        item = _match_label(spec, tokenized)
        if item is not None:
            by_label[spec.id] = item
            used_paths.add(item.path)

    for spec in schema.fields:
        item = by_label.get(spec.id)
        label_text: str | None = None
        resolution = "label"
        raw_value = ""

        if item is not None:
            label, _sep, rest = _split_inline_label(item.value)
            label_text = label
            raw_value = rest if label is not None else item.value
        elif spec.path is not None:
            path = spec.path_tuple
            assert path is not None
            # A nested path like 3.1 falls back to its parent item when the schema
            # defines sub-fields but the reporter wrote evidence as one block.
            positional = tokenized.item_at(path) if path not in used_paths else None
            if positional is None and len(path) > 1:
                parent = tokenized.item_at((path[0],))
                if parent is not None and parent.path not in used_paths:
                    positional = parent
            resolution = "ordinal"
            if positional is not None:
                item = positional
                raw_value = positional.value
                used_paths.add(positional.path)
                # A positional hit whose text is itself a label means the label
                # form was used but with different wording; accept it.
                label, _sep, rest = _split_inline_label(positional.value)
                if label is not None and is_blank_value(rest):
                    raw_value = ""

        evidence_slot = _attachment_slot(schema, spec)

        if item is None:
            if spec.accepts_attachments and attachment_count > evidence_slot:
                result = parse_attachment("", spec.id, attachment_count, evidence_slot)
                bound.append(
                    BoundField(
                        spec=spec,
                        item_path=None,
                        label_text=None,
                        resolution="attachment_only",
                        raw_value="",
                        result=result,
                    )
                )
                continue
            bound.append(
                BoundField(
                    spec=spec,
                    item_path=None,
                    label_text=None,
                    resolution="absent",
                    raw_value="",
                    result=FieldResult(value=None, present=False),
                )
            )
            continue

        result = _parse_by_type(spec, raw_value, attachment_count, evidence_slot)
        bound.append(
            BoundField(
                spec=spec,
                item_path=item.path,
                label_text=label_text,
                resolution=resolution,
                raw_value=raw_value,
                result=result,
            )
        )

    return bound


def _attachment_slot(schema: FormSchemaDef, spec: FieldSpec) -> int:
    """Which attachment index this evidence field may claim.

    Ordered by declaration, so "3.1" takes the first photo and "3.2" the second.
    """
    index = 0
    for candidate in schema.fields:
        if candidate.id == spec.id:
            return index
        if candidate.accepts_attachments:
            index += 1
    return index


def _parse_by_type(spec: FieldSpec, raw: str, attachment_count: int, slot: int = 0) -> FieldResult:
    match spec.type:
        case "identity":
            return parse_identity(
                raw,
                spec.id,
                spec.formations,
                spec.lore_prefixes,
                spec.lore_prefix_formations,
            )
        case "formation_idn":
            return parse_formation_idn(raw, spec.id, spec.formations)
        case "enum":
            return parse_enum(raw, spec.id, spec.options, strict=spec.strict_options)
        case "discord_message_ref":
            return parse_discord_link(raw, spec.id)
        case "attachment_required":
            return parse_attachment(raw, spec.id, attachment_count, slot)
        case "count_fraction":
            return parse_count_fraction(raw, spec.id)
        case "name_ref":
            return parse_name_ref(raw, spec.id)
        case _:
            return parse_text(raw)


# --- validation ------------------------------------------------------------


def validate(
    schema: FormSchemaDef, fields: list[BoundField], tokenized: Tokenized | None = None
) -> list[FieldIssue]:
    """Check required-ness, conditional requirements, and cross-field rules."""
    issues: list[FieldIssue] = []
    values = {b.spec.id: b for b in fields}

    def field_value(field_id: str) -> Any:
        """A single scalar for comparisons: the enum code, else the raw value."""
        bound = values.get(field_id)
        if bound is None or bound.result.value is None:
            return None
        value = bound.result.value
        if not isinstance(value, dict):
            return value
        for key in ("code", "formation", "idn_number", "got", "raw"):
            if value.get(key) is not None:
                return value[key]
        return None

    def value_code(field_id: str) -> str | None:
        raw = field_value(field_id)
        return str(raw) if raw is not None else None

    def attachment_total() -> int:
        total = 0
        for bound in fields:
            value = bound.result.value
            if isinstance(value, dict):
                total = max(total, int(value.get("attachment_count") or 0))
        return total

    for bound in fields:
        spec = bound.spec
        needed = spec.required
        if spec.required_when:
            for other_field, accepted in spec.required_when.items():
                other = value_code(other_field)
                if other is not None and other.upper() in {a.upper() for a in accepted}:
                    needed = True

        present = (
            bound.result.present
            and (bound.result.value is not None or bound.spec.accepts_attachments)
        ) or _has_attachment_evidence(bound)

        # A field the reporter wrote but that failed validation is NOT missing: it has
        # its own, more specific error. Reporting both is noise and hides the real fix.
        if needed and not present and not bound.result.present:
            issues.append(
                FieldIssue(
                    code="field_missing",
                    field_id=spec.id,
                    detail={"label": spec.labels[0] if spec.labels else spec.id},
                )
            )

    issues.extend(_links_in_wrong_field(schema, fields, tokenized))

    # --- cross-field rules -------------------------------------------------
    # These are what make hard forms expressible: requirements that depend on another
    # field's value cannot live on a single field.
    #
    # Two different situations, deliberately handled differently:
    #
    #   * "at least one photo" is a property of the FORM, not of the qualification.
    #     It applies even when the qualification was not understood, so it lives on the
    #     photos field itself (required=True) and always fires.
    #   * "2 photos" and "the link is required" depend on a *recognised* qualification.
    #     If that field was not understood, those rules prove nothing, so they are
    #     skipped: reporting them would hide the real error behind noise.
    scalar_values = {b.spec.id: field_value(b.spec.id) for b in fields}
    unresolved: set[str] = set()
    for bound in fields:
        value = bound.result.value
        matched = value.get("matched") if isinstance(value, dict) else None
        if matched is False:
            unresolved.add(bound.spec.id)

    for rule in schema.rules:
        if any(condition.field in unresolved for condition in rule.when):
            continue
        if not rule.applies(scalar_values):
            continue

        bound = values.get(rule.field)
        if bound is None:
            continue

        present = bound.result.present and (
            bound.result.value is not None or _has_attachment_evidence(bound)
        )

        if rule.min_attachments is not None:
            available = attachment_total()
            # When the field is entirely absent it is already reported as a missing
            # field. Adding "need at least 2, found 0" on top says the same thing twice.
            if available < rule.min_attachments and bound.result.present:
                issues.append(
                    FieldIssue(
                        code="rule_min_attachments",
                        field_id=rule.field,
                        detail={
                            "rule": rule.id,
                            "required": rule.min_attachments,
                            "found": available,
                            "message": _rule_message(rule, schema.locale),
                        },
                    )
                )
                continue

        if rule.max_attachments is not None:
            available = attachment_total()
            if available > rule.max_attachments:
                exact = rule.exactly_attachments
                issues.append(
                    FieldIssue(
                        code="rule_exact_attachments"
                        if exact is not None
                        else "rule_max_attachments",
                        field_id=rule.field,
                        detail={
                            "rule": rule.id,
                            "required": exact if exact is not None else rule.max_attachments,
                            "found": available,
                            "message": _rule_message(rule, schema.locale),
                        },
                    )
                )
                continue

        if rule.allowed:
            current = value_code(rule.field)
            allowed = {a.upper() for a in rule.allowed}
            if current is not None and current.upper() not in allowed:
                issues.append(
                    FieldIssue(
                        code="rule_not_allowed",
                        field_id=rule.field,
                        detail={
                            "rule": rule.id,
                            "found": current,
                            "allowed": rule.allowed,
                            "message": _rule_message(rule, schema.locale),
                        },
                    )
                )
                continue

        if rule.type is not None and present:
            value = bound.result.value
            actual_type = value.get("type") if isinstance(value, dict) else None
            if actual_type is not None and actual_type != rule.type:
                issues.append(
                    FieldIssue(
                        code="rule_wrong_type",
                        field_id=rule.field,
                        detail={
                            "rule": rule.id,
                            "expected": rule.type,
                            "found": actual_type,
                            "message": _rule_message(rule, schema.locale),
                        },
                    )
                )
                continue

        # Required only while the rule applies; absence is a violation.
        if rule.min_attachments is None and not present:
            issues.append(
                FieldIssue(
                    code="rule_required",
                    field_id=rule.field,
                    detail={"rule": rule.id, "message": _rule_message(rule, schema.locale)},
                )
            )

    return issues


def _rule_message(rule: Rule, locale: str) -> str:
    if not rule.message:
        return rule.id
    return rule.message.get(locale) or next(iter(rule.message.values()))


def _number_link_issues(fields: list[BoundField], issues: list[FieldIssue]) -> None:
    """Give every link issue the numbered field the reporter must fix.

    The message used to say "field 4" as literal text, which was already wrong for any
    schema whose link lives elsewhere, and said nothing about *which* link is wanted. The
    number is filled in from the spec so the wording cannot drift from the form.
    """
    numbers = {bound.spec.id: _field_number(bound.spec) for bound in fields}
    for issue in issues:
        if issue.code not in {"link_not_a_url", "link_not_discord", "link_in_wrong_field"}:
            continue
        if issue.field_id is None:
            continue
        issue.detail.setdefault("field_number", numbers.get(issue.field_id, "?"))


def _drop_doubled_link_complaints(fields: list[BoundField], issues: list[FieldIssue]) -> None:
    """Do not say the same thing twice about the promotion link.

    Three checks can fire together: "text instead of a photo" (the field holds a link),
    "the link belongs in item 4" (it is in the wrong place), and "a link is required"
    (item 4 is empty). All three are true, but the last two are consequences of the
    first, and listing them makes a one-line fix look like three problems.
    """
    misplaced = {
        issue.detail.get("found_in") for issue in issues if issue.code == "link_in_wrong_field"
    }
    if not misplaced:
        return

    # "A link is required" says nothing new when the link has been found elsewhere - but
    # only for the field the link was found for. Filtering by the issue *code* alone would
    # silently swallow an unrelated required-field error as soon as a form has a second
    # conditional requirement.
    link_fields = {issue.field_id for issue in issues if issue.code == "link_in_wrong_field"}

    def is_redundant(issue: FieldIssue) -> bool:
        if issue.code == "rule_required":
            return issue.field_id in link_fields
        if issue.code == "evidence_text_without_file" and issue.field_id is not None:
            numbers = {bound.spec.id: _field_number(bound.spec) for bound in fields}
            return numbers.get(issue.field_id) in misplaced
        return False

    issues[:] = [issue for issue in issues if not is_redundant(issue)]


def _links_in_wrong_field(
    schema: FormSchemaDef,
    fields: list[BoundField],
    tokenized: Tokenized | None = None,
) -> list[FieldIssue]:
    """A Discord link written in the wrong numbered field.

    The failure this exists for is quiet and misleading. Somebody pastes the previous
    report's link into field 3 instead of 4; field 3 is the photos field, which accepts
    text, so the link is swallowed without complaint and the report is judged **valid**.
    The promotion link is then simply absent, and nothing said so.

    Worse, if photos *are* attached the report validates anyway, so the mistake only
    surfaces months later when somebody cannot find the evidence chain.

    So: if a field that does not accept links holds one, and the schema has a field that
    does, say so and name the field number to move it to. Only reported when the correct
    field is actually empty - if the reporter put the link in both places the right one is
    already there and there is nothing to fix.
    """
    link_field = next((bound for bound in fields if bound.spec.type == "discord_message_ref"), None)
    if link_field is None or link_field.result.present:
        return []

    expected = _field_number(link_field.spec)
    issues: list[FieldIssue] = []
    # Sub-item text is not kept on the bound field, so `3.1. <link>` is invisible to a
    # check on `raw_value` alone. Scan the tokenized items as well, which is where the
    # reporter's actual text lives.
    for bound in fields:
        if bound.spec.type == "discord_message_ref":
            continue
        if not _accepts_links(bound.spec):
            continue
        link_text = _link_in_field(bound, tokenized)
        if link_text is None:
            continue
        issues.append(
            FieldIssue(
                code="link_in_wrong_field",
                field_id=link_field.spec.id,
                detail={
                    "found_in": _field_number(bound.spec),
                    "expected": expected,
                    "found_raw": link_text,
                },
            )
        )
    return issues


def _link_in_field(bound: BoundField, tokenized: Tokenized | None) -> str | None:
    """The link held by this field, including one written as a sub-item.

    `raw_value` is only the field's own text. Evidence is often written as
    `3. Доказательства:` followed by `3.1. <link>`, and the sub-item lives in a separate
    token, so checking the head alone missed the link entirely.
    """
    for candidate in (
        bound.raw_value,
        (bound.result.value or {}).get("raw") if isinstance(bound.result.value, dict) else None,
    ):
        if isinstance(candidate, str) and _looks_like_link(candidate):
            return candidate.strip()

    if tokenized is None:
        return None
    prefix = bound.item_path or bound.spec.path_tuple
    if not prefix:
        return None
    for item in tokenized.items:
        if len(item.path) <= len(prefix) or item.path[: len(prefix)] != prefix:
            continue
        if _looks_like_link(item.value):
            return item.value.strip()
    return None


def _accepts_links(spec: FieldSpec) -> bool:
    """Whether this field type can legitimately hold a link.

    An attachment field is text-capable (a reporter may write the photo count there), so a
    link pasted into it is stored rather than rejected - which is exactly how it goes
    missing. The identity field is excluded because a link there fails its own validation
    loudly.
    """
    return spec.accepts_attachments or spec.type in {"text", "name_ref"}


def _looks_like_link(raw: str) -> bool:
    value = (raw or "").strip()
    if not value:
        return False
    # A bare URL, or a link preceded by a label such as "Доказательства:".
    return "http://" in value or "https://" in value


def _field_number(spec: FieldSpec) -> str:
    """The number the reporter sees, e.g. "4" from path 4 or "3.1" from path 3.1."""
    path = (spec.path or "").strip()
    if not path:
        return spec.labels[0] if spec.labels else spec.id
    return path.split(".")[0]


def _has_attachment_evidence(bound: BoundField) -> bool:
    value = bound.result.value
    if not isinstance(value, dict):
        return False
    return int(value.get("attachment_count") or 0) > 0


# --- top-level parse -------------------------------------------------------


def parse_report(
    schemas: list[FormSchemaDef],
    group: ReportCandidate,
    *,
    attachment_count: int | None = None,
) -> ParseOutput:
    """Parse a message group into one report, or report why nothing matched.

    Only the *first* message contributes structure. Follow-up messages exist to carry
    attachments, and their text must never be spliced into the report: joining message
    contents lets a stray "123" get absorbed into an empty numbered field, which
    actually happened with real data. Attachment counts are summed across the group.
    """
    tokenized = tokenize(group.primary.content)
    schema, score, scores = select_schema(schemas, group.primary.content)

    if schema is None:
        return ParseOutput(
            matched=False,
            schema_key=None,
            score=score,
            issues=[FieldIssue(code="schema_unmatched", field_id=None, severity="error")],
            diagnostics={"scores": scores, "items": len(tokenized.items)},
        )

    total_attachments = (
        attachment_count if attachment_count is not None else group.total_attachments
    )
    fields = bind_fields(schema, tokenized, total_attachments)
    issues = validate(schema, fields, tokenized)
    for bound in fields:
        issues.extend(bound.result.issues)
    # Number the link issues once, after both sets are merged: the field-level checks
    # ("no link here") are added above, not inside `validate`, so numbering them there
    # would miss them and leave a raw `{detail_field_number}` in the warning.
    _number_link_issues(fields, issues)
    _drop_doubled_link_complaints(fields, issues)

    attribution_raw: str | None = None
    if schema.attribution_field:
        bound_attr = next((b for b in fields if b.spec.id == schema.attribution_field), None)
        if bound_attr is not None and isinstance(bound_attr.result.value, dict):
            attribution_raw = bound_attr.result.value.get("raw")

    diagnostics: dict[str, Any] = {
        "scores": scores,
        "items": len(tokenized.items),
        "preamble_len": len(tokenized.preamble),
        "bindings": [
            {
                "field": b.spec.id,
                "path": ".".join(str(p) for p in b.item_path) if b.item_path else None,
                "resolution": b.resolution,
                "present": b.result.present,
            }
            for b in fields
        ],
    }

    return ParseOutput(
        matched=True,
        schema_key=schema.key,
        score=score,
        fields=fields,
        issues=issues,
        attribution_raw=attribution_raw,
        attribution_field=schema.attribution_field,
        diagnostics=diagnostics,
    )

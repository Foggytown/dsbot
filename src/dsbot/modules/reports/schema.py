"""Report form schemas, defined as data.

A schema never contains code. Adding a form is adding a row; changing a form is
editing JSON. The parser is compiled from these definitions, which is why a second
channel with a different numbering needs no deploy.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

FieldType = Literal[
    "text",
    "long_text",
    "enum",
    "formation_idn",
    "discord_message_ref",
    "attachment_required",
    "count_fraction",
    "name_ref",
    "any",
    "identity",
]

# A condition on a *rule*, e.g. "2+ photos when qualification is not T".
# Kept deliberately simple so it can be written by hand in YAML.
CompareOp = Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte"]


class Condition(BaseModel):
    """One comparison against another field's parsed value."""

    field: str
    op: CompareOp = "eq"
    value: Any = None

    def matches(self, actual: Any) -> bool:
        if self.op == "eq":
            return _cmp(actual) == _cmp(self.value)
        if self.op == "ne":
            return _cmp(actual) != _cmp(self.value)
        if self.op == "in":
            allowed = self.value if isinstance(self.value, list) else [self.value]
            return _cmp(actual) in {_cmp(v) for v in allowed}
        if self.op == "not_in":
            allowed = self.value if isinstance(self.value, list) else [self.value]
            return _cmp(actual) not in {_cmp(v) for v in allowed}
        if self.op in ("gt", "gte", "lt", "lte"):
            left = _num(actual)
            right = _num(self.value)
            if left is None or right is None:
                return False
            if self.op == "gt":
                return left > right
            if self.op == "gte":
                return left >= right
            if self.op == "lt":
                return left < right
            return left <= right
        return False


def _cmp(value: Any) -> str:
    return str(value).strip().upper() if value is not None else ""


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Rule(BaseModel):
    """A validation requirement that can depend on other fields.

    Rules are what make hard forms expressible: "number 4 must be a link when the
    qualification is not T" is a rule, not a field property.
    """

    id: str
    field: str
    # Human sentence explaining the rule, shown to the reporter and to admins.
    message: dict[str, str] = Field(default_factory=dict)
    # All conditions must hold for this rule to apply. Empty means "always".
    when: list[Condition] = Field(default_factory=list)
    # Requirement to apply when the rule is active.
    min_count: int | None = None
    # At least this many attachments.
    min_attachments: int | None = None
    # At most this many attachments. Use with min_attachments for "exactly N".
    max_attachments: int | None = None
    # Value must be one of these.
    allowed: list[str] = Field(default_factory=list)
    type: str | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> Rule:
        if (
            self.min_attachments is not None
            and self.max_attachments is not None
            and self.min_attachments > self.max_attachments
        ):
            raise ValueError(
                f"rule '{self.id}': min_attachments ({self.min_attachments}) is greater "
                f"than max_attachments ({self.max_attachments})"
            )
        return self

    @property
    def exactly_attachments(self) -> int | None:
        """Convenience for the common 'exactly N' case."""
        if self.min_attachments is not None and self.min_attachments == self.max_attachments:
            return self.min_attachments
        return None

    def applies(self, values: dict[str, Any]) -> bool:
        return all(c.matches(values.get(c.field)) for c in self.when)


class FieldSpec(BaseModel):
    """One numbered item in a form."""

    id: str
    # Human labels this field may be titled with. Matching is normalised and fuzzy,
    # so "Название квалификации" also matches "квалификация".
    labels: list[str] = Field(default_factory=list)
    # Numeric position. 3 means item "3"; "3.1" becomes path (3, 1).
    path: str | None = None
    type: FieldType = "text"
    required: bool = False
    # Conditional requirement: {other_field_id: [acceptable values]}
    required_when: dict[str, list[str]] | None = None
    # For enum fields. Seeded from real data, admin-editable.
    options: list[str] = Field(default_factory=list)
    # When true, a value outside `options` is an error rather than a warning. A wrong
    # qualification breaks quota counting, so this defaults to strict.
    strict_options: bool = True
    # For formation_idn fields: the closed set of formations. Formations are finite and
    # known (SOB, ARC, RC, Reaper, 666th, ...). The IDN that follows is NOT finite and
    # is never checked against a list.
    formations: list[str] = Field(default_factory=list)
    # Number of attachments this field consumes. 0 means it is text-only.
    attachment_slots: int = 0
    # Allowed prefixes for a lore IDN such as 01/0016. Only these combinations are
    # valid in this community, so anything else is rejected rather than stored.
    lore_prefixes: list[str] = Field(default_factory=lambda: ["01", "00"])
    # Only these formations may use a prefixed IDN at all. Every other formation must
    # use a plain 4-digit IDN, because 01/ and 00/ describe SOB squads specifically.
    lore_prefix_formations: list[str] = Field(default_factory=lambda: ["SOB"])
    # Value may legitimately be an attachment rather than text.
    accepts_attachments: bool = False
    # Free-text note shown to a reporter in the warning message.
    hint: dict[str, str] = Field(default_factory=dict)

    @field_validator("type", mode="before")
    @classmethod
    def _normalise_type(cls, value: Any) -> Any:
        """Accept friendlier aliases so hand-written YAML stays readable."""
        if not isinstance(value, str):
            return value
        aliases = {
            "attachment": "attachment_required",
            "photo": "attachment_required",
            "photos": "attachment_required",
            "link": "discord_message_ref",
            "url": "discord_message_ref",
            "choice": "enum",
            "list": "enum",
        }
        return aliases.get(value.strip().lower(), value.strip().lower())

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split(".")
        if not all(p.isdigit() for p in parts):
            raise ValueError(f"path must be dotted digits, got {value!r}")
        return value

    @property
    def path_tuple(self) -> tuple[int, ...] | None:
        if self.path is None:
            return None
        return tuple(int(p) for p in self.path.split("."))

    @property
    def ordinal(self) -> int | None:
        if self.path is None:
            return None
        return int(self.path.split(".")[0])


class FormSchemaDef(BaseModel):
    """A complete form definition."""

    key: str
    title: str = ""
    locale: str = "ru"
    # Distinctive phrases that must appear in the template but never in a real
    # report. Used only to recognise the template itself, not to parse reports.
    template_markers: list[str] = Field(default_factory=list)
    # Phrases a real report is likely to contain. Used to score schema candidates
    # when one channel hosts several forms.
    anchors: list[str] = Field(default_factory=list)
    fields: list[FieldSpec]
    # Cross-field requirements that a single field cannot express.
    rules: list[Rule] = Field(default_factory=list)
    # Which field names the person this report counts for. Unset means "the author".
    attribution_field: str | None = None
    # When true, a report credited to someone other than its author needs a staff
    # verdict even when the form validates.
    attribution_requires_review: bool = False
    min_score: float = 1.0

    def field_by_id(self, field_id: str) -> FieldSpec | None:
        for f in self.fields:
            if f.id == field_id:
                return f
        return None

    def to_definition(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def training_report_schema() -> FormSchemaDef:
    """The training report form, transcribed from the live template.

    Field numbering mirrors the template. Field 3's sub-items carry the evidence
    photos, which may arrive in a follow-up message from the same author.
    """
    return FormSchemaDef(
        key="training_report",
        title="Формуляр рапорта на обучение",
        locale="ru",
        template_markers=[
            "формуляр рапорта на обучение",
            "используйте подобный формат",
            "Alpha: 01/00",
            "Null: 00/00",
        ],
        anchors=[
            "формирование",
            "квалификаци",
            "доказательства",
            "обучаем",
        ],
        fields=[
            FieldSpec(
                id="formation_idn",
                labels=["формирование", "формирование | rp-idn", "rp-idn", "nrп"],
                path="1",
                type="formation_idn",
                required=True,
                # Formations are a finite, known set. The IDN after the pipe is
                # open-ended and is never validated against a list.
                formations=[
                    "SOB",
                    "ARC",
                    "RC",
                    "REAPER",
                    "666TH",
                    "NULL",
                    "ALPHA",
                ],
                hint={
                    "ru": "Укажите формирование и IDN: «SOB | 0016» или «ARC | 01/0016».",
                    "en": "Provide formation and IDN: 'SOB | 0016' or 'ARC | 01/0016'.",
                },
            ),
            FieldSpec(
                id="qualification",
                labels=["название квалификации", "квалификация", "квалиф"],
                path="2",
                type="enum",
                required=True,
                # Qualifications a soldier is trained FOR. Only INF is confirmed so far;
                # add others by editing forms/ch-<id>.json and re-importing. SOB is a
                # formation, not a qualification.
                options=["INF"],
                hint={
                    "ru": "Укажите название квалификации (например, INF).",
                    "en": "Provide the qualification name (for example, INF).",
                },
            ),
            FieldSpec(
                id="evidence_1",
                labels=["фото в инж. блоке", "фото в инженерном блоке", "инж. блок"],
                path="3.1",
                type="attachment_required",
                accepts_attachments=True,
                required=True,
                hint={
                    "ru": "Приложите фото в инженерном блоке.",
                    "en": "Attach the photo from the engineering block.",
                },
            ),
            FieldSpec(
                id="evidence_2",
                labels=["фото практики", "фото практики во время обучения"],
                path="3.2",
                type="attachment_required",
                accepts_attachments=True,
                required=True,
                hint={
                    "ru": "Приложите фото практики во время обучения.",
                    "en": "Attach the practice photo taken during training.",
                },
            ),
            FieldSpec(
                id="promotion_link",
                labels=["ссылку на одобренное повышение", "повышение квалификации", "ссылка"],
                path="4",
                type="discord_message_ref",
                required=False,
                required_when={"qualification": ["ENG", "INF+", "ENG+"]},
                hint={
                    "ru": "Для ENG/INF+ приложите ссылку на одобренное повышение квалификации.",
                    "en": "For ENG/INF+, attach a link to the approved qualification upgrade.",
                },
            ),
        ],
        attribution_field=None,
    )


DEFAULT_SCHEMAS: tuple[FormSchemaDef, ...] = (training_report_schema(),)


def from_definition(definition: dict[str, Any]) -> FormSchemaDef:
    """Rebuild a schema from its stored JSON.

    Schemas live in the database and are edited by admins, so the parser must accept
    a definition it has never seen as source code.
    """
    return FormSchemaDef.model_validate(definition)

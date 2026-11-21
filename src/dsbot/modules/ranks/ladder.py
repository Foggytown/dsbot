"""Rank ladders, defined as data.

A ladder is an ordered list of tiers. There are two kinds, and the difference matters:

  purpose: review   who may APPROVE a report. Capabilities live here.
  purpose: quota    who OWES reports, and how many per week. No capabilities at all.

They are separate files because approving and reporting are different jobs: a role that
owes three reports a week must not thereby be allowed to approve anything.

    # review.yaml - authority only
    purpose: review
    tiers:
      - key: senior_instructor
        ordinal: 30
        roles: [123456789]
        capabilities: [review]

    # quota.yaml - duties only
    purpose: quota
    tiers:
      - key: senior_instructor
        ordinal: 30
        roles: [123456789]
        required_per_week: 4

`ordinal` orders people within a ladder. In a review ladder it answers "whose verdict
outranks whose". In a quota ladder it decides which requirement applies to someone who
holds several quota roles: the **highest** tier wins, so a person is never asked to file
both a senior and a junior quota.

The same shape as forms on purpose: an admin edits YAML, `check` validates it, and
applying it never requires a code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Capabilities a tier may hold. Anything else is rejected so a typo cannot silently
# grant nothing.
CAPABILITIES: tuple[str, ...] = (
    "review",  # may approve or reject a report
    "admin",  # may configure forms, ladders and quotas
    "quota_manage",  # may change weekly requirements
)

PURPOSES: tuple[str, ...] = ("review", "quota")

DEFAULT_LADDER_DIR = Path("forms/ladders")


class TierSpec(BaseModel):
    key: str
    label: str = ""
    # Higher wins. Two tiers may not share an ordinal in the same ladder.
    ordinal: int
    # Discord role ids from THIS guild. Empty is allowed while scaffolding a ladder;
    # `ladder_problems` reports it so `ladder-check` explains why it will not match.
    roles: list[int] = Field(default_factory=list)
    # Only meaningful in a review ladder.
    capabilities: list[str] = Field(default_factory=list)
    # Only meaningful in a quota ladder: reports this tier must file each week.
    # None means "not specified", which is different from 0 ("no requirement").
    required_per_week: int | None = None

    @field_validator("capabilities")
    @classmethod
    def _known_capabilities(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in CAPABILITIES]
        if unknown:
            raise ValueError(f"unknown capabilities {unknown}; allowed: {list(CAPABILITIES)}")
        return value

    @field_validator("required_per_week")
    @classmethod
    def _sane_requirement(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("required_per_week cannot be negative")
        return value


class LadderSpec(BaseModel):
    """One guild's ordering, for one purpose."""

    key: str
    guild_id: int
    name: str = ""
    # Declared, never inferred: an unfinished review ladder must not be mistaken for a
    # quota ladder, and vice versa.
    purpose: str = "review"
    tiers: list[TierSpec]

    @field_validator("purpose")
    @classmethod
    def _known_purpose(cls, value: str) -> str:
        if value not in PURPOSES:
            raise ValueError(f"purpose must be one of {list(PURPOSES)}, got {value!r}")
        return value

    @model_validator(mode="after")
    def _ordinals_are_unique(self) -> LadderSpec:
        ordinals = [t.ordinal for t in self.tiers]
        duplicates = {o for o in ordinals if ordinals.count(o) > 1}
        if duplicates:
            raise ValueError(
                f"ladder '{self.key}': duplicate ordinals {sorted(duplicates)}. "
                "Equal ordinals would make precedence ambiguous."
            )
        if not self.tiers:
            raise ValueError(f"ladder '{self.key}' has no tiers")
        return self

    @model_validator(mode="after")
    def _roles_are_unique(self) -> LadderSpec:
        seen: dict[int, str] = {}
        for tier in self.tiers:
            for role in tier.roles:
                if role in seen:
                    raise ValueError(
                        f"ladder '{self.key}': role {role} appears in both "
                        f"'{seen[role]}' and '{tier.key}'"
                    )
                seen[role] = tier.key
        return self

    @model_validator(mode="after")
    def _purpose_matches_fields(self) -> LadderSpec:
        """A quota tier that grants review would let a reporter approve itself.

        This is the one thing that must never be ambiguous, so it is an error rather
        than a warning: the mistake is silent otherwise and corrupts every verdict.
        """
        if self.purpose == "quota":
            offenders = [t.key for t in self.tiers if t.capabilities]
            if offenders:
                raise ValueError(
                    f"ladder '{self.key}' has purpose 'quota' but tiers {offenders} list "
                    "capabilities. Who owes a report must not be able to approve one; "
                    "put those roles in the review ladder instead."
                )
        return self

    @property
    def review_ordinal(self) -> int | None:
        """Lowest ordinal that may review, for a quick capability check."""
        reviewable = [t.ordinal for t in self.tiers if "review" in t.capabilities]
        return min(reviewable) if reviewable else None


def load_ladder(path: Path) -> LadderSpec:
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return LadderSpec.model_validate(data)


def load_ladders(directory: Path = DEFAULT_LADDER_DIR) -> list[LadderSpec]:
    """Every ladder file. `.yaml.example` files are templates and are not loaded."""
    if not directory.is_dir():
        return []
    ladders: list[LadderSpec] = []
    for path in sorted(directory.glob("*.yaml")):
        ladders.append(load_ladder(path))
    return ladders


def ladder_problems(spec: LadderSpec) -> list[str]:
    """Catch the mistakes an admin actually makes when writing a ladder."""
    problems: list[str] = []
    if spec.purpose == "review" and not any("review" in t.capabilities for t in spec.tiers):
        problems.append(
            f"ladder '{spec.key}': no tier has the 'review' capability, "
            "so nobody could approve a report"
        )
    if spec.purpose == "review" and any(t.required_per_week is not None for t in spec.tiers):
        problems.append(
            f"ladder '{spec.key}' is a review ladder but sets required_per_week. "
            "Weekly requirements belong in the quota ladder, or they will be ignored here."
        )
    if spec.purpose == "quota" and not any(t.required_per_week is not None for t in spec.tiers):
        problems.append(
            f"ladder '{spec.key}': no tier sets required_per_week, so it demands nothing. "
            "Add how many reports each tier must file each week."
        )
    if spec.purpose == "review" and not any("admin" in t.capabilities for t in spec.tiers):
        problems.append(f"ladder '{spec.key}': no tier has the 'admin' capability")
    for tier in spec.tiers:
        if tier.ordinal <= 0:
            problems.append(f"tier '{tier.key}': ordinal must be positive, got {tier.ordinal}")
        if not tier.label:
            problems.append(f"tier '{tier.key}': add a label for the report output")
        if not tier.roles:
            problems.append(
                f"tier '{tier.key}': no roles listed, so nobody can be matched to it. "
                "Add the Discord role ids from this guild."
            )
    return problems

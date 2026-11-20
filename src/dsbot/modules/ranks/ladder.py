"""Rank ladders, defined as data.

A ladder is an ordered list of tiers. The ordinal answers exactly one question: whose
verdict outranks whose. Permissions are separate (`capabilities`), so authority and
administrative power can never be confused by accident.

Roles live in the *specialization* servers, not the report server, so each tier lists
Discord role ids from its own guild. That is why a ladder is bound to one guild:

    guild_id: 1274460259661910149      # the specialization server
    tiers:
      - key: senior_instructor
        label: Senior Instructor
        ordinal: 30
        roles: [123456789, 234567890]

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

DEFAULT_LADDER_DIR = Path("forms/ladders")


class TierSpec(BaseModel):
    key: str
    label: str = ""
    # Higher wins. Two tiers may not share an ordinal in the same ladder.
    ordinal: int
    # Discord role ids from THIS guild. Empty is allowed while scaffolding a ladder;
    # `ladder_problems` reports it so `ladder-check` explains why it will not match.
    roles: list[int] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("capabilities")
    @classmethod
    def _known_capabilities(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in CAPABILITIES]
        if unknown:
            raise ValueError(f"unknown capabilities {unknown}; allowed: {list(CAPABILITIES)}")
        return value


class LadderSpec(BaseModel):
    """One guild's authority ordering."""

    key: str
    guild_id: int
    name: str = ""
    # What this ladder is for. `review` orders who may approve (and needs a review
    # capability), `quota` lists who owes reports (and deliberately has none). The
    # distinction has to be explicit: inferring it from capabilities would make an
    # unfinished review ladder look like a quota ladder and silently disable approvals.
    purpose: str = "review"
    tiers: list[TierSpec]

    @field_validator("purpose")
    @classmethod
    def _known_purpose(cls, value: str) -> str:
        allowed = {"review", "quota"}
        if value not in allowed:
            raise ValueError(f"purpose must be one of {sorted(allowed)}, got {value!r}")
        return value

    @model_validator(mode="after")
    def _ordinals_are_unique(self) -> LadderSpec:
        ordinals = [t.ordinal for t in self.tiers]
        duplicates = {o for o in ordinals if ordinals.count(o) > 1}
        if duplicates:
            raise ValueError(
                f"ladder '{self.key}': duplicate ordinals {sorted(duplicates)}. "
                "Equal ordinals would make verdict precedence ambiguous."
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
    if spec.purpose == "quota" and any("review" in t.capabilities for t in spec.tiers):
        problems.append(
            f"ladder '{spec.key}': a quota ladder must not grant 'review', "
            "or everyone who owes a report could also approve one"
        )
    if not any("admin" in t.capabilities for t in spec.tiers):
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

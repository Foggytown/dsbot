"""Ladder validation: the mistakes an admin actually makes."""

from __future__ import annotations

from pathlib import Path

import pytest

from dsbot.modules.ranks.ladder import (
    LadderSpec,
    TierSpec,
    ladder_problems,
    load_ladder,
    load_ladders,
)


def tier(
    key: str,
    ordinal: int,
    roles: list[int] | None = None,
    capabilities: list[str] | None = None,
) -> TierSpec:
    return TierSpec(
        key=key,
        label=key.replace("_", " ").title(),
        ordinal=ordinal,
        roles=roles if roles is not None else [1],
        capabilities=capabilities or [],
    )


def test_minimal_ladder_parses() -> None:
    spec = LadderSpec(
        key="test",
        guild_id=1,
        tiers=[tier("head", 50, [1], ["review", "admin"]), tier("low", 10, [2])],
    )
    assert spec.review_ordinal == 50


def test_duplicate_ordinals_are_rejected() -> None:
    """Equal ordinals would make precedence ambiguous, so they are a hard error."""
    with pytest.raises(ValueError, match="duplicate ordinals"):
        LadderSpec(
            key="test",
            guild_id=1,
            tiers=[tier("a", 10, [1]), tier("b", 10, [2])],
        )


def test_one_role_cannot_be_in_two_tiers() -> None:
    with pytest.raises(ValueError, match="appears in both"):
        LadderSpec(
            key="test",
            guild_id=1,
            tiers=[tier("a", 10, [7]), tier("b", 20, [7])],
        )


def test_unknown_capability_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown capabilities"):
        tier("a", 10, [1], ["superuser"])


def test_empty_ladder_is_rejected() -> None:
    with pytest.raises(ValueError, match="no tiers"):
        LadderSpec(key="test", guild_id=1, tiers=[])


def test_monotonic_is_not_required() -> None:
    """Tiers may be written in any order; ordinal decides, not position."""
    spec = LadderSpec(
        key="test",
        guild_id=1,
        tiers=[tier("low", 10, [1]), tier("high", 90, [2])],
    )
    assert max(t.ordinal for t in spec.tiers) == 90


def test_problems_mention_missing_review_capability() -> None:
    spec = LadderSpec(key="test", guild_id=1, tiers=[tier("a", 10, [1], ["admin"])])
    problems = ladder_problems(spec)
    assert any("review" in p for p in problems)


def test_problems_mention_empty_roles() -> None:
    spec = LadderSpec(key="test", guild_id=1, tiers=[tier("a", 10, [], ["review"])])
    problems = ladder_problems(spec)
    assert any("no roles listed" in p for p in problems)


def test_clean_ladder_has_no_problems() -> None:
    spec = LadderSpec(
        key="test",
        guild_id=1,
        tiers=[
            tier("head", 50, [1], ["review", "admin"]),
            tier("low", 10, [2], ["review"]),
        ],
    )
    assert ladder_problems(spec) == []


def test_shipped_review_example_parses_but_needs_roles() -> None:
    """The example is a skeleton: valid to load, incomplete until roles are added."""
    path = Path("forms/ladders/review.yaml.example")
    spec = load_ladder(path)
    assert spec.key == "review"
    problems = ladder_problems(spec)
    assert any("no roles listed" in p for p in problems)


def test_shipped_quota_example_grants_no_review() -> None:
    """The quota ladder must never contain a review capability.

    If it did, everyone who owes a report could also approve one, which is exactly the
    confusion the two files exist to prevent.
    """
    path = Path("forms/ladders/quota.yaml.example")
    spec = load_ladder(path)
    assert spec.key == "quota"
    assert not any("review" in tier.capabilities for tier in spec.tiers)
    # It is still a usable quota ladder: the problems must be about roles and admin only.
    problems = ladder_problems(spec)
    assert any("no roles listed" in p for p in problems)
    assert not any("no tier has the 'review'" in p for p in problems if "quota" in p)


def test_missing_directory_returns_no_ladders(tmp_path: Path) -> None:
    assert load_ladders(tmp_path / "nope") == []

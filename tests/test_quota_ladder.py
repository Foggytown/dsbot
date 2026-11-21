"""Weekly requirements live in the quota ladder, not in a separate table.

These pin the rules that decide a person's number, because an off-by-one here silently
changes what everyone owes:

  * `required_per_week` is a property of the rank;
  * a quota ladder cannot grant capabilities;
  * someone holding several quota roles gets the HIGHEST requirement, never the sum.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dsbot.modules.ranks.ladder import LadderSpec, TierSpec, ladder_problems, load_ladder
from dsbot.modules.ranks.resolver import GuildLadders, LadderIndex


def quota_ladder(*tiers: TierSpec) -> LadderSpec:
    return LadderSpec(key="quota", guild_id=900, purpose="quota", tiers=list(tiers))


def test_required_per_week_is_read_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "quota.yaml"
    path.write_text(
        "key: quota\npurpose: quota\nguild_id: 1\ntiers:\n"
        "  - key: instructor\n    ordinal: 10\n    roles: [5]\n    required_per_week: 3\n",
        encoding="utf-8",
    )
    spec = load_ladder(path)
    assert spec.tiers[0].required_per_week == 3


def test_quota_ladder_cannot_grant_capabilities() -> None:
    """The single mistake that would let a reporter approve itself."""
    with pytest.raises(ValueError) as excinfo:
        quota_ladder(TierSpec(key="instructor", ordinal=10, roles=[1], capabilities=["review"]))
    message = str(excinfo.value)
    assert "quota" in message and "capabilities" in message


def test_review_ladder_with_a_requirement_is_refused() -> None:
    """Weekly norms belong in the quota ladder; in the review file they would be ignored."""
    with pytest.raises(ValueError) as excinfo:
        LadderSpec(
            key="review",
            guild_id=900,
            purpose="review",
            tiers=[
                TierSpec(
                    key="senior",
                    label="Senior",
                    ordinal=30,
                    roles=[1],
                    capabilities=["review", "admin"],
                    required_per_week=2,
                )
            ],
        )
    assert "required_per_week" in str(excinfo.value)


def test_quota_ladder_cannot_even_carry_the_capabilities_field() -> None:
    """An empty list is still a mistake: a meaningless field gets filled in later."""
    with pytest.raises(ValueError) as excinfo:
        quota_ladder(
            TierSpec(
                key="instructor",
                label="Instructor",
                ordinal=10,
                roles=[1],
                required_per_week=3,
                capabilities=[],
            )
        )
    assert "capabilities" in str(excinfo.value)


def test_quota_ladder_with_no_requirements_is_flagged() -> None:
    spec = quota_ladder(TierSpec(key="instructor", label="Instructor", ordinal=10, roles=[1]))
    assert any("no tier sets required_per_week" in p for p in ladder_problems(spec))


def test_negative_requirement_is_rejected() -> None:
    with pytest.raises(ValueError):
        TierSpec(key="x", ordinal=1, roles=[1], required_per_week=-1)


def group_with_quota(*entries: tuple[int, int, int, str]) -> GuildLadders:
    """Each entry: (role_id, ordinal, required, label)."""
    group = GuildLadders(guild_id=900)
    group.add(
        LadderIndex(
            ladder_id=2,
            guild_id=900,
            key="quota",
            purpose="quota",
            by_role={
                role: (f"t{ordinal}", label, ordinal, frozenset())
                for role, ordinal, _required, label in entries
            },
            quota_by_role={role: required for role, _o, required, _l in entries},
        )
    )
    return group


def test_highest_quota_tier_wins_not_the_sum() -> None:
    """A promotion must not stack a second requirement on top of the first."""
    group = group_with_quota(
        (10, 30, 4, "Senior Instructor"),
        (20, 10, 3, "Instructor"),
    )
    required, label, ordinal = group.required_for_roles(frozenset({10, 20}))
    assert required == 4
    assert label == "Senior Instructor"
    assert ordinal == 30


def test_requirement_of_the_only_role_is_used() -> None:
    group = group_with_quota((20, 10, 3, "Instructor"))
    required, _label, _ordinal = group.required_for_roles(frozenset({20}))
    assert required == 3


def test_someone_with_no_quota_role_owes_nothing() -> None:
    group = group_with_quota((20, 10, 3, "Instructor"))
    required, label, ordinal = group.required_for_roles(frozenset({999}))
    assert (required, label, ordinal) == (0, None, None)


def test_a_review_role_never_supplies_a_requirement() -> None:
    """Review authority and reporting duty are separate, in both directions."""
    group = GuildLadders(guild_id=900)
    group.add(
        LadderIndex(
            ladder_id=1,
            guild_id=900,
            key="review",
            purpose="review",
            by_role={10: ("head", "Head", 50, frozenset({"review"}))},
        )
    )
    required, _label, _ordinal = group.required_for_roles(frozenset({10}))
    assert required == 0


def test_shipped_quota_example_sets_requirements() -> None:
    spec = load_ladder(Path("forms/ladders/quota.yaml.example"))
    assert spec.purpose == "quota"
    assert all(tier.required_per_week is not None for tier in spec.tiers)
    assert not any(tier.capabilities for tier in spec.tiers)


def test_shipped_review_example_sets_no_requirements() -> None:
    spec = load_ladder(Path("forms/ladders/review.yaml.example"))
    assert spec.purpose == "review"
    assert all(tier.required_per_week is None for tier in spec.tiers)
    assert any("review" in tier.capabilities for tier in spec.tiers)


async def test_roster_stores_the_requirement_from_the_ladder(tmp_path: Path) -> None:
    """The frozen requirement is what counting reads, not a live lookup."""
    from dsbot.config import Settings
    from dsbot.modules.ranks.ladder import LadderSpec as Spec
    from dsbot.modules.ranks.ladder import TierSpec as Tier
    from dsbot.modules.ranks.roster import RosterService, StaticRosterSource
    from dsbot.storage import ranks_repo
    from dsbot.storage.db import Database

    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    db = Database(settings)
    try:
        await db.prepare()
        async with db.write_session() as session:
            ladder = await ranks_repo.replace_ladder(
                session,
                Spec(
                    key="quota",
                    guild_id=900,
                    purpose="quota",
                    tiers=[
                        Tier(
                            key="senior",
                            label="Senior",
                            ordinal=30,
                            roles=[10],
                            required_per_week=4,
                        ),
                        Tier(
                            key="junior",
                            label="Junior",
                            ordinal=10,
                            roles=[20],
                            required_per_week=2,
                        ),
                    ],
                ),
            )
        async with db.session() as session:
            group = (await ranks_repo.load_all_ladders(session))[900]

        class Member:
            def __init__(self, uid: int, roles: list[int], name: str) -> None:
                self.id = uid
                self.roles = [type("R", (), {"id": r})() for r in roles]
                self.display_name = name

        service = RosterService(
            db,
            StaticRosterSource({900: [Member(1, [10, 20], "Both"), Member(2, [20], "Junior")]}),
        )
        result = await service.refresh(900, group, ladder_id=ladder.id)
        assert result.ok

        stored = {row["user_id"]: row for row in await service.load(ladder.id)}
        assert stored[1]["required_per_week"] == 4, "highest tier wins"
        assert stored[2]["required_per_week"] == 2
    finally:
        await db.dispose()


async def test_counting_uses_the_stored_requirement(tmp_path: Path) -> None:
    """End to end: the ladder's number reaches the report."""
    from dsbot.config import Settings
    from dsbot.modules.quotas.counting import CountSummary, MemberTally
    from dsbot.modules.quotas.period import Period
    from dsbot.modules.quotas.service import CountingService, CountRequest
    from dsbot.modules.ranks.ladder import LadderSpec as Spec
    from dsbot.modules.ranks.ladder import TierSpec as Tier
    from dsbot.modules.reports.schema import FormSchemaDef
    from dsbot.modules.reports.yaml_form import parse_form_file
    from dsbot.storage import admin, ranks_repo, repos
    from dsbot.storage.db import Database

    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    db = Database(settings)
    try:
        await db.prepare()
        schema: FormSchemaDef = parse_form_file(Path("forms/training.yaml"))
        async with db.write_session() as session:
            await admin.ensure_schema_seed(session, [schema])
            binding = await admin.bind_channel(
                session,
                guild_id=900,
                channel_id=1000,
                module_key="reports",
                schema_key=schema.key,
                rank_source_guild_id=900,
            )
            ladder = await ranks_repo.replace_ladder(
                session,
                Spec(
                    key="quota",
                    guild_id=900,
                    purpose="quota",
                    tiers=[
                        Tier(
                            key="senior",
                            label="Senior",
                            ordinal=30,
                            roles=[10],
                            required_per_week=4,
                        )
                    ],
                ),
            )
            await repos.replace_roster(
                session,
                ladder.id,
                [
                    {
                        "user_id": 1,
                        "display_name": "Ann",
                        "tier_label": "Senior",
                        "tier_ordinal": 30,
                        "required_per_week": 4,
                    }
                ],
            )

        outcome = await CountingService(db).count(
            CountRequest(
                binding_id=binding.id,
                channel_id=1000,
                period=Period(
                    start=datetime(2026, 11, 16, tzinfo=UTC),
                    end=datetime(2026, 11, 23, tzinfo=UTC),
                ),
            )
        )
        summary: CountSummary = outcome.summary
        ann = next(t for t in summary.tallies if t.user_id == 1)
        assert ann.required_count == 4
        assert isinstance(ann, MemberTally)
        assert ann.met is False
    finally:
        await db.dispose()


async def test_review_roles_survive_a_coexisting_quota_ladder(tmp_path: Path) -> None:
    """A guild with a review ladder and a quota ladder must keep its review authority.

    The bug: the count that feeds the `ladders_loaded` diagnostic indexed the by_role
    *tuple* instead of its capabilities slot, so `"review" in entry` was always False and
    the log said review_roles=0 while approvals worked.
    """
    from dsbot.config import Settings
    from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
    from dsbot.storage import ranks_repo
    from dsbot.storage.db import Database

    shared_role = 111
    database = Database(
        Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")  # type: ignore[call-arg]
    )
    await database.prepare()
    try:
        async with database.write_session() as session:
            await ranks_repo.replace_ladder(
                session,
                LadderSpec(
                    key="review",
                    guild_id=900,
                    purpose="review",
                    tiers=[
                        TierSpec(
                            key="senior",
                            label="Senior",
                            ordinal=30,
                            roles=[shared_role],
                            capabilities=["review"],
                        )
                    ],
                ),
            )
            await ranks_repo.replace_ladder(
                session,
                LadderSpec(
                    key="quota",
                    guild_id=900,
                    purpose="quota",
                    tiers=[
                        TierSpec(
                            key="senior",
                            label="Senior",
                            ordinal=30,
                            roles=[shared_role],
                            required_per_week=2,
                        )
                    ],
                ),
            )

        async with database.session() as session:
            group = (await ranks_repo.load_all_ladders(session))[900]
    finally:
        await database.dispose()

    review_roles = sum(
        1
        for index in group.indexes
        if index.purpose == "review"
        for entry in index.by_role.values()
        if "review" in entry[3]
    )
    assert review_roles == 1, "the review ladder must still grant review"

    # And the merged view is the one that misleads: quota wins for the shared role.
    merged = sum(1 for entry in group.by_role.values() if "review" in entry[3])
    assert merged == 0, "this is why counting from by_role reported zero"

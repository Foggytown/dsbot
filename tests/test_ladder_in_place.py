"""Editing a ladder file must not destroy what an admin configured.

`replace_ladder` used to delete the ladder row whenever its content changed, and four
things reference it: `roster_members` and `quota_policies` both cascade, so one edited
number in `quota.yaml` silently discarded every `/dsbot quota-set` override and the whole
roster. The weekly report then claimed nobody was tracked.

These tests pin the in-place behaviour, including the two cases that make naive updates
fail: swapping two ordinals under `UNIQUE(ladder_id, ordinal)`, and removing a tier that a
channel override still points at (that foreign key has no `ON DELETE` rule).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select

from dsbot.config import Settings
from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.storage import admin, ranks_repo, repos
from dsbot.storage.db import Database
from dsbot.storage.models import RankTier

GUILD = 900
CHANNEL = 901


def ladder(*tiers: TierSpec) -> LadderSpec:
    return LadderSpec(key="quota", guild_id=GUILD, purpose="quota", tiers=list(tiers))


def tier(key: str, ordinal: int, *, roles: list[int], required: int = 1) -> TierSpec:
    return TierSpec(
        key=key,
        label=key.title(),
        ordinal=ordinal,
        roles=roles,
        required_per_week=required,
    )


@pytest.fixture
async def bound(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'ladder.db'}"))
    await db.create_all()
    await db.prepare()
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
        await admin.bind_channel(
            session,
            guild_id=GUILD,
            channel_id=CHANNEL,
            module_key="reports",
            schema_key="training_report",
        )
        await ranks_repo.replace_ladder(session, ladder(tier("instr", 10, roles=[111])))
    yield db
    await db.dispose()


async def ladder_id(db: Database) -> int:
    async with db.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
    assert group.quota_ladder_id is not None
    return group.quota_ladder_id


async def overrides(db: Database, lid: int) -> list[int]:
    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
        assert binding is not None
        rows = await repos.get_quota_policies(session, ladder_id=lid, binding_id=binding.id)
    return [row.required_count for row in rows]


async def test_editing_one_number_keeps_the_channel_override(bound: Database) -> None:
    """The bug: 3 -> 4 in the YAML deleted the admin's override."""
    lid = await ladder_id(bound)
    async with bound.write_session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
        assert binding is not None
        await admin.set_quota(
            session, ladder_id=lid, tier_id=None, binding_id=binding.id, required_count=5
        )
    assert await overrides(bound, lid) == [5]

    async with bound.write_session() as session:
        await ranks_repo.replace_ladder(session, ladder(tier("instr", 10, roles=[111], required=4)))

    assert await ladder_id(bound) == lid, "the ladder row must survive an edit"
    assert await overrides(bound, lid) == [5], "the channel override must survive"


async def test_editing_a_ladder_keeps_the_roster(bound: Database) -> None:
    """Rebuilding the ladder cascaded the roster away, emptying the weekly report."""
    lid = await ladder_id(bound)
    async with bound.write_session() as session:
        await repos.replace_roster(
            session,
            lid,
            [
                {
                    "user_id": 4242,
                    "display_name": "Tracked",
                    "tier_label": "Instr",
                    "tier_ordinal": 10,
                    "required_per_week": 1,
                }
            ],
        )

    async with bound.write_session() as session:
        await ranks_repo.replace_ladder(session, ladder(tier("instr", 10, roles=[111], required=2)))

    async with bound.session() as session:
        roster = await repos.list_roster(session, lid)
    assert len(roster) == 1, "editing the file must not empty the roster"


async def test_swapping_two_ordinals_is_allowed(bound: Database) -> None:
    """`UNIQUE(ladder_id, ordinal)` forbids a naive swap, so ordinals are parked first."""
    async with bound.write_session() as session:
        await ranks_repo.replace_ladder(
            session,
            ladder(tier("high", 10, roles=[111]), tier("low", 20, roles=[222])),
        )

    async with bound.write_session() as session:
        await ranks_repo.replace_ladder(
            session,
            ladder(tier("high", 20, roles=[111]), tier("low", 10, roles=[222])),
        )

    async with bound.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
    ordinals = {entry[0]: entry[2] for entry in group.indexes[0].by_role.values()}
    assert ordinals == {"high": 20, "low": 10}


async def test_removing_an_overridden_tier_does_not_raise(bound: Database) -> None:
    """`quota_policies.tier_id` has no ON DELETE, so the reference must be released."""
    async with bound.write_session() as session:
        await ranks_repo.replace_ladder(
            session,
            ladder(tier("keep", 10, roles=[111]), tier("drop", 20, roles=[222])),
        )
    async with bound.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
        lid = group.quota_ladder_id
        assert lid is not None
        binding = await repos.get_binding_by_channel(session, CHANNEL)
        assert binding is not None
        tiers = (await session.scalars(select(RankTier).where(RankTier.ladder_id == lid))).all()
        dropped = next(row for row in tiers if row.key == "drop")
    async with bound.write_session() as session:
        await admin.set_quota(
            session, ladder_id=lid, tier_id=dropped.id, binding_id=binding.id, required_count=9
        )

    # Removing the tier must not raise a foreign-key error.
    async with bound.write_session() as session:
        await ranks_repo.replace_ladder(session, ladder(tier("keep", 10, roles=[111])))

    async with bound.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
    remaining = {entry[0] for entry in group.indexes[0].by_role.values()}
    assert remaining == {"keep"}


async def test_adding_a_role_to_a_tier_keeps_overrides(bound: Database) -> None:
    lid = await ladder_id(bound)
    async with bound.write_session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
        assert binding is not None
        await admin.set_quota(
            session, ladder_id=lid, tier_id=None, binding_id=binding.id, required_count=5
        )

    async with bound.write_session() as session:
        await ranks_repo.replace_ladder(session, ladder(tier("instr", 10, roles=[111, 222])))

    assert await overrides(bound, lid) == [5]


async def test_unchanged_content_is_not_rewritten(bound: Database) -> None:
    """A redeploy runs this; identical content must cost nothing and change nothing."""
    lid = await ladder_id(bound)
    for _ in range(3):
        async with bound.write_session() as session:
            await ranks_repo.replace_ladder(session, ladder(tier("instr", 10, roles=[111])))
    assert await ladder_id(bound) == lid

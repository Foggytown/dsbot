"""Removing a tier must be loud, and must not leave a useless override behind.

Updating a ladder in place changed the failure mode of a removed tier. It used to cascade
the roster away, which was destructive but at least visible ("roster is empty"). Now the
roster row survives with its old label and requirement - correct, because that requirement
was already applied to past weeks - but the person keeps appearing in the weekly table as
owing reports under a tier nobody can see in the ladder any more. That has to be said out
loud, or it is the "silence is the worst failure" mistake again.

The override row is a different matter: it can never match anybody, so it is deleted
rather than parked as `tier_id = NULL`. A NULL row keys on None while roster rows always
carry a numeric ordinal, so it is litter that reads like a global default.
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
from dsbot.storage.models import QuotaPolicy, RankTier

GUILD = 940
CHANNEL = 941


def ladder(*tiers: TierSpec) -> LadderSpec:
    return LadderSpec(key="quota", guild_id=GUILD, purpose="quota", tiers=list(tiers))


def tier(key: str, ordinal: int, *, roles: list[int], required: int) -> TierSpec:
    return TierSpec(
        key=key, label=key.title(), ordinal=ordinal, roles=roles, required_per_week=required
    )


@pytest.fixture
async def two_tiers(tmp_path: Path) -> AsyncIterator[tuple[Database, int]]:
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}"))
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
        await ranks_repo.replace_ladder(
            session,
            ladder(
                tier("keep", 10, roles=[111], required=2),
                tier("drop", 30, roles=[222], required=4),
            ),
        )
    async with db.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
    assert group.quota_ladder_id is not None
    yield db, group.quota_ladder_id
    await db.dispose()


async def _tier_id(db: Database, key: str) -> int:
    async with db.session() as session:
        row = await session.scalar(select(RankTier).where(RankTier.key == key))
    assert row is not None
    return row.id


async def test_removing_a_tier_deletes_its_override(two_tiers: tuple[Database, int]) -> None:
    """Not `tier_id = NULL`: such a row can never match a roster row."""
    db, ladder_id = two_tiers
    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
    assert binding is not None
    async with db.write_session() as session:
        await admin.set_quota(
            session,
            ladder_id=ladder_id,
            tier_id=await _tier_id(db, "drop"),
            binding_id=binding.id,
            required_count=9,
        )
    assert (await _overrides(db)) == [(9)]

    async with db.write_session() as session:
        await ranks_repo.replace_ladder(session, ladder(tier("keep", 10, roles=[111], required=2)))

    assert await _overrides(db) == [], "the override must be gone, not parked as NULL"


async def test_a_removed_tier_warns_on_every_start(two_tiers: tuple[Database, int]) -> None:
    """The people stay counted - their past weeks were judged - but silently is not allowed.

    The warning used to live inside `_sync_tiers`, so it fired once, when the ladder changed,
    and never again while the state persisted. That is the same silence, just delayed, so
    the check runs on every start now and keeps saying so until somebody re-scrapes.
    """
    from structlog.testing import capture_logs

    db, ladder_id = two_tiers
    async with db.write_session() as session:
        await repos.replace_roster(
            session,
            ladder_id,
            [
                {
                    "user_id": 7,
                    "tier_key": "keep",
                    "tier_label": "Keep",
                    "tier_ordinal": 10,
                    "required_per_week": 2,
                },
                {
                    "user_id": 8,
                    "tier_key": "drop",
                    "tier_label": "Drop",
                    "tier_ordinal": 30,
                    "required_per_week": 4,
                },
            ],
        )

    async with db.write_session() as session:
        await ranks_repo.replace_ladder(session, ladder(tier("keep", 10, roles=[111], required=2)))

    # Every subsequent start must warn again, because the state has not changed.
    for start in range(3):
        with capture_logs() as events:
            async with db.session() as session:
                stale_rows = await ranks_repo.report_stale_roster(session)
        hits = [event for event in events if event.get("event") == "roster_stale_tiers"]
        assert hits, f"start {start + 1} said nothing about the stale tier"
        assert hits[0]["keys"] == ["drop"]
        assert stale_rows == 1

    # The roster keeps the stale row on purpose: its requirement was already applied.
    async with db.session() as session:
        roster = await repos.list_roster(session, ladder_id)
    assert {row.user_id for row in roster} == {7, 8}


async def test_no_warning_when_every_tier_still_exists(two_tiers: tuple[Database, int]) -> None:
    from structlog.testing import capture_logs

    db, ladder_id = two_tiers
    async with db.write_session() as session:
        await repos.replace_roster(
            session,
            ladder_id,
            [
                {
                    "user_id": 7,
                    "tier_key": "keep",
                    "tier_label": "Keep",
                    "tier_ordinal": 10,
                    "required_per_week": 2,
                }
            ],
        )

    with capture_logs() as events:
        async with db.session() as session:
            stale_rows = await ranks_repo.report_stale_roster(session)

    assert stale_rows == 0
    assert not [event for event in events if event.get("event") == "roster_stale_tiers"]


async def test_each_ladder_reports_its_own_row_count(tmp_path: Path) -> None:
    """A shared `rows` total made every line overstate its own evidence.

    Two ladders with one stale row each both logged `rows=2`, so the line pointed at a
    number that only existed across ladders. A log that proves something other than what it
    says is the same class of error as a diagnostic reporting zero while the feature works.
    """
    from structlog.testing import capture_logs

    from dsbot.storage import admin, ranks_repo, repos
    from dsbot.storage.db import Database

    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'multi.db'}"))
    await db.create_all()
    try:
        async with db.write_session() as session:
            await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
            await admin.bind_channel(
                session,
                guild_id=GUILD,
                channel_id=CHANNEL,
                module_key="reports",
                schema_key="training_report",
            )
            for key, role in (("q1", 111), ("q2", 222)):
                await ranks_repo.replace_ladder(
                    session,
                    LadderSpec(
                        key=key,
                        guild_id=GUILD,
                        purpose="quota",
                        tiers=[
                            TierSpec(
                                key="gone",
                                label="Gone",
                                ordinal=10,
                                roles=[role],
                                required_per_week=1,
                            )
                        ],
                    ),
                )
        async with db.session() as session:
            group = (await ranks_repo.load_all_ladders(session))[GUILD]
            ladder_ids = [index.ladder_id for index in group.indexes if index.ladder_id]

        # Two stale rows on each ladder, so a shared total would say 4 on both lines.
        for ladder_id in ladder_ids:
            async with db.write_session() as session:
                await repos.replace_roster(
                    session,
                    ladder_id,
                    [
                        {
                            "user_id": user,
                            "tier_key": "gone",
                            "tier_label": "Gone",
                            "tier_ordinal": 10,
                            "required_per_week": 1,
                        }
                        for user in (7, 8)
                    ],
                )
        async with db.write_session() as session:
            for key, role in (("q1", 111), ("q2", 222)):
                await ranks_repo.replace_ladder(
                    session,
                    LadderSpec(
                        key=key,
                        guild_id=GUILD,
                        purpose="quota",
                        tiers=[
                            TierSpec(
                                key="keep",
                                label="Keep",
                                ordinal=10,
                                roles=[role],
                                required_per_week=1,
                            )
                        ],
                    ),
                )

        with capture_logs() as events:
            async with db.session() as session:
                total = await ranks_repo.report_stale_roster(session)

        assert total == 4, "the return value is the overall count"
        lines = [e for e in events if e.get("event") == "roster_stale_tiers"]
        assert len(lines) == 2
        for line in lines:
            assert line["rows"] == 2, f"each line must count its own ladder, got {line}"
            assert line["untiered"] == 0
    finally:
        await db.dispose()


async def _overrides(db: Database) -> list[int]:
    async with db.session() as session:
        rows = (await session.scalars(select(QuotaPolicy))).all()
    return [row.required_count for row in rows]

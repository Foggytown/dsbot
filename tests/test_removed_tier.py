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


async def test_a_removed_tier_warns_about_stale_roster_rows(
    two_tiers: tuple[Database, int],
) -> None:
    """The people stay counted - their past weeks were judged - but silently is not allowed."""

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

    # structlog does not go through `logging` unless configured to, so use its own capture.
    from structlog.testing import capture_logs

    with capture_logs() as events:
        async with db.write_session() as session:
            await ranks_repo.replace_ladder(
                session, ladder(tier("keep", 10, roles=[111], required=2))
            )

    # The roster keeps the stale row on purpose: its requirement was already applied.
    async with db.session() as session:
        roster = await repos.list_roster(session, ladder_id)
    assert {row.user_id for row in roster} == {7, 8}

    stale = [event for event in events if event.get("event") == "roster_stale_tiers"]
    assert stale, f"a removed tier must be reported, got {[e.get('event') for e in events]}"
    assert stale[0]["keys"] == ["drop"]
    assert stale[0]["rows"] == 1


async def _overrides(db: Database) -> list[int]:
    async with db.session() as session:
        rows = (await session.scalars(select(QuotaPolicy))).all()
    return [row.required_count for row in rows]

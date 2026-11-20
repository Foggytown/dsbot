"""Ladder persistence: apply, replace, and reload indexed ladders."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select

from dsbot.config import Settings
from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.storage import ranks_repo
from dsbot.storage.db import Database
from dsbot.storage.models import RankLadder, RankTier, RankTierRole


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'ranks.db'}")
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


def spec(ordinal_head: int = 50) -> LadderSpec:
    return LadderSpec(
        key="instructors",
        guild_id=900,
        name="Instructors",
        tiers=[
            TierSpec(
                key="head",
                label="Head Instructor",
                ordinal=ordinal_head,
                roles=[10],
                capabilities=["review", "admin"],
            ),
            TierSpec(
                key="senior",
                label="Senior Instructor",
                ordinal=30,
                roles=[20],
                capabilities=["review"],
            ),
        ],
    )


async def test_apply_creates_ladder_tiers_roles(db: Database) -> None:
    async with db.write_session() as session:
        ladder = await ranks_repo.replace_ladder(session, spec())

    async with db.session() as session:
        stored = await session.get(RankLadder, ladder.id)
        assert stored is not None
        assert stored.guild_id == 900
        tiers = (
            await session.scalars(select(RankTier).where(RankTier.ladder_id == ladder.id))
        ).all()
        assert len(tiers) == 2
        roles = (await session.scalars(select(RankTierRole))).all()
        assert {r.role_id for r in roles} == {10, 20}


async def test_reapply_replaces_instead_of_duplicating(db: Database) -> None:
    """A ladder is one authority ordering; a second apply must not leave stale tiers."""
    async with db.write_session() as session:
        await ranks_repo.replace_ladder(session, spec(ordinal_head=50))
    async with db.write_session() as session:
        await ranks_repo.replace_ladder(session, spec(ordinal_head=60))

    async with db.session() as session:
        ladders = (await session.scalars(select(RankLadder))).all()
        assert len(ladders) == 1
        tiers = (
            await session.scalars(select(RankTier).where(RankTier.ladder_id == ladders[0].id))
        ).all()
        head = next(t for t in tiers if t.key == "head")
        assert head.ordinal == 60


async def test_capabilities_are_stored(db: Database) -> None:
    async with db.write_session() as session:
        await ranks_repo.replace_ladder(session, spec())

    async with db.session() as session:
        ladders = await ranks_repo.list_ladders(session)
    assert len(ladders) == 1
    _ladder, rows = ladders[0]
    head = next(r for r in rows if r["key"] == "head")
    assert set(head["capabilities"]) == {"review", "admin"}


async def test_load_all_ladders_builds_role_index(db: Database) -> None:
    async with db.write_session() as session:
        await ranks_repo.replace_ladder(session, spec())

    async with db.session() as session:
        by_guild = await ranks_repo.load_all_ladders(session)

    assert 900 in by_guild
    index = by_guild[900]
    tier_key, _label, ordinal, caps = index.by_role[20]
    assert tier_key == "senior"
    assert ordinal == 30
    assert "review" in caps


async def test_index_resolves_a_member_to_a_tier(db: Database) -> None:
    """End to end: stored ladder -> resolver -> verdict input."""
    from dsbot.modules.ranks.resolver import RankResolver
    from dsbot.modules.ranks.source import StaticRankSource

    async with db.write_session() as session:
        await ranks_repo.replace_ladder(session, spec())
    async with db.session() as session:
        by_guild = await ranks_repo.load_all_ladders(session)

    resolver = RankResolver(StaticRankSource({(900, 7): frozenset({20})}), by_guild)
    rank = await resolver.resolve(900, 7)
    assert rank is not None
    assert rank.tier_key == "senior"
    assert rank.can_review


async def test_ladder_for_guild(db: Database) -> None:
    async with db.write_session() as session:
        await ranks_repo.replace_ladder(session, spec())
    async with db.session() as session:
        found = await ranks_repo.ladder_for_guild(session, 900)
        missing = await ranks_repo.ladder_for_guild(session, 12345)
    assert found is not None
    assert missing is None

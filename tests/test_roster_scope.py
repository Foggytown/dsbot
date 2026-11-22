"""The roster is the list of people who owe reports, not everyone with any role.

A guild has a review ladder and a quota ladder. The roster used to be resolved against
every ladder at once, so somebody holding *only* a review role was matched, labelled with
their review tier, and appeared in the weekly table with a quota of 0. That is noise in a
report whose whole point is "who did not file".

Decided explicitly: a person with only review roles must be absent. A person with a quota
role appears even with a requirement of 0 - they are on the ladder, and an admin can hide
that tier with `/dsbot hide-tier` if they do not want to see it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from dsbot.config import Settings
from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.ranks.roster import RosterService, RosterSource
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.storage import admin, ranks_repo
from dsbot.storage.db import Database

GUILD = 920
CHANNEL = 921
REVIEW_ROLE = 9001
QUOTA_ROLE = 9002
ZERO_QUOTA_ROLE = 9003


class _Member:
    def __init__(self, user_id: int, roles: list[int], name: str) -> None:
        self.id = user_id
        self.roles = [type("R", (), {"id": role})() for role in roles]
        self.display_name = name


class _Source(RosterSource):
    def __init__(self, members: list[_Member]) -> None:
        self._members = members

    def supports_roster(self) -> bool:
        return True

    async def members_with_roles(self, guild_id: int) -> list[Any]:
        return list(self._members)


@pytest.fixture
async def two_ladders(tmp_path: Path) -> AsyncIterator[tuple[Database, int]]:
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'roster.db'}"))
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
            LadderSpec(
                key="review",
                guild_id=GUILD,
                purpose="review",
                tiers=[
                    TierSpec(
                        key="senior",
                        label="Senior Instructor",
                        ordinal=30,
                        roles=[REVIEW_ROLE],
                        capabilities=["review"],
                    )
                ],
            ),
        )
        await ranks_repo.replace_ladder(
            session,
            LadderSpec(
                key="quota",
                guild_id=GUILD,
                purpose="quota",
                tiers=[
                    TierSpec(
                        key="instructor",
                        label="Instructor",
                        ordinal=10,
                        roles=[QUOTA_ROLE],
                        required_per_week=1,
                    ),
                    TierSpec(
                        key="observer",
                        label="Observer",
                        ordinal=5,
                        roles=[ZERO_QUOTA_ROLE],
                        required_per_week=0,
                    ),
                ],
            ),
        )
    async with db.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
    assert group.quota_ladder_id is not None
    yield db, group.quota_ladder_id
    await db.dispose()


async def test_a_review_only_person_is_not_in_the_roster(
    two_ladders: tuple[Database, int],
) -> None:
    """The bug: they appeared with a review tier label and a quota of 0."""
    db, ladder_id = two_ladders
    async with db.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
    service = RosterService(
        db,
        _Source(
            [
                _Member(1, [REVIEW_ROLE], "Reviewer Only"),
                _Member(2, [QUOTA_ROLE], "Quota Person"),
            ]
        ),
    )

    result = await service.refresh(GUILD, group, ladder_id=ladder_id)

    ids = {row["user_id"] for row in result.members}
    assert 1 not in ids, "a review-only role must not put somebody in the weekly table"
    assert 2 in ids


async def test_somebody_with_both_roles_is_counted_once(
    two_ladders: tuple[Database, int],
) -> None:
    db, ladder_id = two_ladders
    async with db.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
    service = RosterService(db, _Source([_Member(3, [REVIEW_ROLE, QUOTA_ROLE], "Both Roles")]))

    result = await service.refresh(GUILD, group, ladder_id=ladder_id)

    assert len(result.members) == 1
    row = result.members[0]
    assert row["user_id"] == 3
    assert row["tier_label"] == "Instructor", "the quota tier names the row, not the review one"
    assert row["required_per_week"] == 1


async def test_a_quota_role_with_zero_requirement_still_appears(
    two_ladders: tuple[Database, int],
) -> None:
    """On the ladder means listed. Hiding a tier is what `/dsbot hide-tier` is for."""
    db, ladder_id = two_ladders
    async with db.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
    service = RosterService(db, _Source([_Member(4, [ZERO_QUOTA_ROLE], "Observer")]))

    result = await service.refresh(GUILD, group, ladder_id=ladder_id)

    assert [row["user_id"] for row in result.members] == [4]
    assert result.members[0]["required_per_week"] == 0

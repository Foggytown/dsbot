"""Proves how quotas come from the ladder: the exact thing that was confusing.

uv run python scripts/e2e-quota.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from dsbot.config import Settings
from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.ranks.roster import RosterService, StaticRosterSource
from dsbot.storage import ranks_repo
from dsbot.storage.db import Database

GUILD = 900
SENIOR_ROLE = 10
INSTRUCTOR_ROLE = 20
REVIEW_ROLE = 30


class Member:
    def __init__(self, uid: int, roles: list[int], name: str) -> None:
        self.id = uid
        self.roles = [type("R", (), {"id": r})() for r in roles]
        self.display_name = name


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'q.db').as_posix()}")
    db = Database(settings)
    try:
        await db.prepare()

        async with db.write_session() as session:
            await ranks_repo.replace_ladder(
                session,
                LadderSpec(
                    key="quota",
                    guild_id=GUILD,
                    purpose="quota",
                    tiers=[
                        TierSpec(
                            key="senior_instructor",
                            label="Senior Instructor",
                            ordinal=30,
                            roles=[SENIOR_ROLE],
                            required_per_week=4,
                        ),
                        TierSpec(
                            key="instructor",
                            label="Instructor",
                            ordinal=10,
                            roles=[INSTRUCTOR_ROLE],
                            required_per_week=3,
                        ),
                    ],
                ),
            )

        async with db.session() as session:
            group = (await ranks_repo.load_all_ladders(session))[GUILD]
            ladder_id = group.quota_ladder_id or 0

        print("Quota ladder tiers:")
        for index in group.indexes:
            for role, req in index.quota_by_role.items():
                print(f"  role {role}: {req}/week")

        print()
        service = RosterService(
            db,
            StaticRosterSource(
                {
                    GUILD: [
                        # Holds BOTH roles: gets 4, not 4+3=7.
                        Member(1, [SENIOR_ROLE, INSTRUCTOR_ROLE], "Ann (both roles)"),
                        Member(2, [INSTRUCTOR_ROLE], "Bob (instructor only)"),
                        # Only a review role: owes nothing, so not in the quota roster.
                        Member(3, [REVIEW_ROLE], "Cid (review only)"),
                    ]
                }
            ),
        )
        result = await service.refresh(GUILD, group, ladder_id=ladder_id)
        print(f"scraped {result.total_members} members, matched {len(result.members)}")

        print()
        for row in await service.load(ladder_id):
            print(
                f"  {row['display_name']:<24} tier={row['tier_label']:<20} "
                f"owes={row['required_per_week']}/week"
            )

        assert {r["display_name"]: r["required_per_week"] for r in result.members} == {
            "Ann (both roles)": 4,
            "Bob (instructor only)": 3,
        }, "highest tier must win, and a review role must not add a requirement"
        print("\nOK: 4 (not 7) for someone holding both, and review roles owe nothing")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

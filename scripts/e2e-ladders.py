"""Verifies the corrected model: separate review and quota ladders, channel pinning.

Run with: uv run python scripts/e2e-ladders.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from dsbot.config import Settings
from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.ranks.resolver import RankResolver
from dsbot.modules.ranks.source import StaticRankSource
from dsbot.storage import ranks_repo
from dsbot.storage.db import Database

GUILD = 1274460259661910149
REVIEW_HEAD = 111
REVIEW_SENIOR = 222
QUOTA_SENIOR = 333
QUOTA_INSTRUCTOR = 444

SOMEONE = 9001


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(database_url=f"sqlite+aiosqlite:///{Path(tmp) / 'ladders.db'}")
        db = Database(settings)
        await db.prepare()

        async with db.write_session() as session:
            await ranks_repo.replace_ladder(
                session,
                LadderSpec(
                    key="review",
                    purpose="review",
                    guild_id=GUILD,
                    name="review",
                    tiers=[
                        TierSpec(
                            key="head",
                            label="Head Instructor",
                            ordinal=50,
                            roles=[REVIEW_HEAD],
                            capabilities=["review"],
                        ),
                        TierSpec(
                            key="senior",
                            label="Senior Instructor",
                            ordinal=30,
                            roles=[REVIEW_SENIOR],
                            capabilities=["review"],
                        ),
                    ],
                ),
            )
            await ranks_repo.replace_ladder(
                session,
                LadderSpec(
                    key="quota",
                    purpose="quota",
                    guild_id=GUILD,
                    name="quota",
                    tiers=[
                        TierSpec(
                            key="senior_q",
                            label="Senior Instructor",
                            ordinal=30,
                            roles=[QUOTA_SENIOR],
                        ),
                        TierSpec(
                            key="instructor",
                            label="Instructor",
                            ordinal=10,
                            roles=[QUOTA_INSTRUCTOR],
                        ),
                    ],
                ),
            )

        async with db.session() as session:
            group = (await ranks_repo.load_all_ladders(session))[GUILD]

        print("ladders in guild:", [i.key for i in group.indexes])
        print("quota ladder id:", group.quota_ladder_id)
        print()

        # 1. A quota role must never approve, even though the guild has a review ladder.
        source = StaticRankSource({(GUILD, SOMEONE): frozenset({QUOTA_SENIOR})})
        resolver = RankResolver(source, {GUILD: group})
        assert await resolver.resolve(GUILD, SOMEONE, requires=frozenset({"review"})) is None
        quota_rank = await resolver.resolve(GUILD, SOMEONE)
        assert quota_rank is not None and quota_rank.tier_key == "senior_q"
        print("quota-only member: cannot review, but is counted ->", quota_rank.tier_label)

        # 2. A review role approves.
        source2 = StaticRankSource({(GUILD, SOMEONE): frozenset({REVIEW_SENIOR})})
        resolver2 = RankResolver(source2, {GUILD: group})
        review_rank = await resolver2.resolve(GUILD, SOMEONE, requires=frozenset({"review"}))
        assert review_rank is not None and review_rank.tier_label == "Senior Instructor"
        print("review member: can approve ->", review_rank.tier_label)

        # 3. Channel pinning: pin to the quota ladder and a review role is not visible
        #    as a reviewer for that channel.
        quota_id = group.quota_ladder_id
        pinned = await resolver2.resolve(
            GUILD, SOMEONE, requires=frozenset({"review"}), ladder_id=quota_id
        )
        assert pinned is None, "a channel pinned to the quota ladder must not inherit review"
        print("channel pinned to quota ladder: no reviewer from the review ladder")

        # 4. Both ladders coexist: the review ladder survived the second apply.
        async with db.session() as session:
            rows = await ranks_repo.list_ladders(session)
        assert sorted(ladder.key for ladder, _ in rows) == ["quota", "review"]
        print("both ladders persisted:", sorted(ladder.key for ladder, _ in rows))

        print("\nOK: quotas and approvals are separated, and channel pinning works")
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

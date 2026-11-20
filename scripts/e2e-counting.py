#!/usr/bin/env python3
"""End-to-end check of the phase 3 pipeline: reactions -> verdict -> count.

Not a test (it is not collected by pytest), but a script you can run after changing the
verdict or counting rules to see real output:

    uv run python scripts/e2e-counting.py

It builds a temporary database, seeds a real form and ladder, stores reactions the same
way the gateway does, and prints the report staff would see. Assertions at the end fail
loudly if the numbers move.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from dsbot.config import Settings
from dsbot.modules.quotas.period import Period
from dsbot.modules.quotas.service import CountingService, CountRequest
from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.ranks.resolver import LadderIndex
from dsbot.modules.ranks.roster import RosterService, StaticRosterSource
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.modules.verdict.service import VerdictService
from dsbot.storage import admin, ranks_repo, repos
from dsbot.storage.db import Database

GUILD = 900
CHANNEL = 1000
HEAD_ROLE = 10
SENIOR_ROLE = 20
INSTRUCTOR_ROLE = 40

ANN = 100  # senior instructor: 2 approved, 1 rejected
BOB = 200  # instructor: 1 undecided
HEAD = 300  # head instructor: files nothing


class _Member:
    def __init__(self, user_id: int, roles: list[int], name: str) -> None:
        self.id = user_id
        self.roles = [type("_Role", (), {"id": role})() for role in roles]
        self.display_name = name


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(database_url=f"sqlite+aiosqlite:///{Path(tmp) / 'e2e.db'}")
        db = Database(settings)
        await db.prepare()

        schema = parse_form_file(Path("forms/training.yaml"))
        async with db.write_session() as session:
            await admin.ensure_schema_seed(session, [schema])
            binding = await admin.bind_channel(
                session,
                guild_id=GUILD,
                channel_id=CHANNEL,
                module_key="reports",
                schema_key=schema.key,
                rank_source_guild_id=GUILD,
            )
            ladder = await ranks_repo.replace_ladder(
                session,
                LadderSpec(
                    key="instructors",
                    guild_id=GUILD,
                    tiers=[
                        TierSpec(
                            key="head",
                            label="Head Instructor",
                            ordinal=50,
                            roles=[HEAD_ROLE],
                            capabilities=["review"],
                        ),
                        TierSpec(
                            key="senior",
                            label="Senior Instructor",
                            ordinal=30,
                            roles=[SENIOR_ROLE],
                            capabilities=["review"],
                        ),
                        TierSpec(
                            key="instructor",
                            label="Instructor",
                            ordinal=10,
                            roles=[INSTRUCTOR_ROLE],
                        ),
                    ],
                ),
            )

        roster = RosterService(
            db,
            StaticRosterSource(
                {
                    GUILD: [
                        _Member(ANN, [SENIOR_ROLE], "Ann"),
                        _Member(BOB, [INSTRUCTOR_ROLE], "Bob"),
                        _Member(HEAD, [HEAD_ROLE], "Head"),
                    ]
                }
            ),
        )
        index = LadderIndex(
            ladder_id=ladder.id,
            guild_id=GUILD,
            key="instructors",
            by_role={
                HEAD_ROLE: ("head", "Head Instructor", 50, frozenset({"review"})),
                SENIOR_ROLE: ("senior", "Senior Instructor", 30, frozenset({"review"})),
                INSTRUCTOR_ROLE: ("instructor", "Instructor", 10, frozenset()),
            },
        )
        await roster.refresh(GUILD, index, ladder_id=ladder.id)

        period = Period(
            start=datetime(2026, 11, 16, tzinfo=UTC),
            end=datetime(2026, 11, 23, tzinfo=UTC),
        )
        verdicts = VerdictService(db)

        for number, author in enumerate([ANN, ANN, ANN, BOB], start=1):
            async with db.write_session() as session:
                report = await repos.upsert_report(
                    session,
                    channel_id=CHANNEL,
                    message_id=number,
                    guild_id=GUILD,
                    author_user_id=author,
                    created_at=period.start.replace(hour=10 + number),
                    binding_id=binding.id,
                    schema_key="training_report",
                )
                report.status = "valid"

        # Head approves Ann's first two reports and rejects the third. Bob's stays open.
        for message_id, emoji in ((1, "\u2705"), (2, "\u2705"), (3, "\u274c")):
            async with db.write_session() as session:
                await repos.add_reaction(
                    session,
                    message_id=message_id,
                    channel_id=CHANNEL,
                    guild_id=GUILD,
                    emoji_key=f"unicode:{emoji}",
                    emoji_id=None,
                    emoji_name=emoji,
                    user_id=HEAD,
                    tier_key="head",
                    tier_ordinal=50,
                    tier_label="Head Instructor",
                    can_review=True,
                )
            update = await verdicts.on_reaction(message_id)
            print(f"report {message_id}: {update.verdict if update else 'not a report'}")

        outcome = await CountingService(db).count(
            CountRequest(binding_id=binding.id, channel_id=CHANNEL, period=period),
            ladder_id=ladder.id,
            ladder=index,
        )
        print()
        for page in outcome.messages():
            print(page)
        print()
        print(outcome.csv())

        tallies = {t.user_id: t for t in outcome.summary.tallies}
        assert tallies[ANN].approved_count == 2, tallies[ANN]
        assert tallies[ANN].rejected_count == 1, tallies[ANN]
        assert tallies[BOB].pending_count == 1, tallies[BOB]
        assert tallies[HEAD].approved_count == 0, tallies[HEAD]
        assert outcome.ok, outcome.problems
        print("OK: counting, verdicts and roster agree")

        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

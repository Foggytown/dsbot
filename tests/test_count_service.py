"""Counting end to end, against a real database.

The pure tests prove the rules. These prove the plumbing: that reactions stored with a
rank snapshot become a verdict, that a roster becomes rows, and that re-running a period
replaces rather than duplicates.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.modules.quotas.period import Period
from dsbot.modules.quotas.service import CountingService, CountRequest
from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.ranks.resolver import GuildLadders, LadderIndex
from dsbot.modules.ranks.roster import RosterService, StaticRosterSource
from dsbot.modules.verdict.service import VerdictService
from dsbot.storage import admin, ranks_repo, repos
from dsbot.storage.db import Database

REPORT_GUILD = 900
CHANNEL = 1000
REVIEWER = 500
AUTHOR = 600
SENIOR_ROLE = 20

PERIOD = Period(
    start=datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
    end=datetime(2026, 11, 23, 0, 0, tzinfo=UTC),
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'count.db'}")
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


def ladder_spec() -> LadderSpec:
    return LadderSpec(
        key="instructors",
        guild_id=REPORT_GUILD,
        name="Instructors",
        tiers=[
            TierSpec(
                key="senior",
                label="Senior Instructor",
                ordinal=30,
                roles=[SENIOR_ROLE],
                capabilities=["review"],
            ),
            TierSpec(key="instructor", label="Instructor", ordinal=10, roles=[30]),
        ],
    )


def guild_ladders(ladder_id: int) -> GuildLadders:
    """The guild's ladder set. One ladder is enough for these tests, but the service
    takes the whole set because a real guild has a review ladder and quota ladders."""
    group = GuildLadders(guild_id=REPORT_GUILD)
    group.add(
        LadderIndex(
            ladder_id=ladder_id,
            guild_id=REPORT_GUILD,
            key="instructors",
            by_role={},
        )
    )
    return group


async def setup_binding(db: Database) -> tuple[int, int]:
    """A channel with a form and a ladder, as a real deployment would have."""
    from dsbot.modules.reports.schema import FormSchemaDef
    from dsbot.modules.reports.yaml_form import parse_form_file

    schema: FormSchemaDef = parse_form_file(Path("forms/training.yaml"))
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, [schema])
        binding = await admin.bind_channel(
            session,
            guild_id=REPORT_GUILD,
            channel_id=CHANNEL,
            module_key="reports",
            schema_key=schema.key,
            rank_source_guild_id=REPORT_GUILD,
        )
        ladder = await ranks_repo.replace_ladder(session, ladder_spec())
    return binding.id, ladder.id


async def add_report(
    db: Database,
    *,
    binding_id: int,
    message_id: int,
    author_id: int,
    created_at: datetime,
) -> int:
    async with db.write_session() as session:
        report = await repos.upsert_report(
            session,
            channel_id=CHANNEL,
            message_id=message_id,
            guild_id=REPORT_GUILD,
            author_user_id=author_id,
            created_at=created_at,
            binding_id=binding_id,
            schema_key="training_report",
        )
        report.status = "valid"
        return report.id


async def add_reaction(
    db: Database,
    *,
    message_id: int,
    user_id: int,
    emoji: str,
    ordinal: int | None,
    can_review: bool | None,
) -> None:
    async with db.write_session() as session:
        await repos.add_reaction(
            session,
            message_id=message_id,
            channel_id=CHANNEL,
            guild_id=REPORT_GUILD,
            emoji_key=f"unicode:{emoji}",
            emoji_id=None,
            emoji_name=emoji,
            user_id=user_id,
            tier_ordinal=ordinal,
            tier_label="Senior Instructor" if ordinal else None,
            can_review=can_review,
        )


async def test_approved_report_counts_for_its_author(db: Database) -> None:
    binding_id, ladder_id = await setup_binding(db)
    await add_report(
        db,
        binding_id=binding_id,
        message_id=1,
        author_id=AUTHOR,
        created_at=PERIOD.start.replace(hour=10),
    )
    await add_reaction(
        db, message_id=1, user_id=REVIEWER, emoji="\u2705", ordinal=30, can_review=True
    )
    async with db.write_session() as session:
        await repos.replace_roster(
            session,
            ladder_id,
            [
                {
                    "user_id": AUTHOR,
                    "display_name": "Ann",
                    "tier_key": "senior",
                    "tier_label": "Senior Instructor",
                    "tier_ordinal": 30,
                }
            ],
        )

    service = CountingService(db)
    outcome = await service.count(
        CountRequest(binding_id=binding_id, channel_id=CHANNEL, period=PERIOD),
        quota_ladder_id=ladder_id,
        ladders=guild_ladders(ladder_id),
    )
    assert outcome.summary.tallies[0].approved_count == 1
    assert outcome.summary.tallies[0].display_name == "Ann"
    assert outcome.ok


async def test_report_outside_the_period_is_not_counted(db: Database) -> None:
    binding_id, ladder_id = await setup_binding(db)
    await add_report(
        db,
        binding_id=binding_id,
        message_id=1,
        author_id=AUTHOR,
        created_at=datetime(2026, 11, 9, 10, tzinfo=UTC),  # previous week
    )

    service = CountingService(db)
    outcome = await service.count(
        CountRequest(binding_id=binding_id, channel_id=CHANNEL, period=PERIOD),
        quota_ladder_id=ladder_id,
    )
    assert outcome.summary.credited_total() == 0


async def test_removing_the_approval_reaction_reverts_the_count(db: Database) -> None:
    """Un-reacting must undo the verdict, which is why state is recomputed."""
    binding_id, _ = await setup_binding(db)
    await add_report(
        db,
        binding_id=binding_id,
        message_id=1,
        author_id=AUTHOR,
        created_at=PERIOD.start.replace(hour=10),
    )
    await add_reaction(
        db, message_id=1, user_id=REVIEWER, emoji="\u2705", ordinal=30, can_review=True
    )

    verdicts = VerdictService(db)
    update = await verdicts.on_reaction(1)
    assert update is not None
    assert update.verdict == "approved"

    async with db.write_session() as session:
        await repos.remove_reaction(
            session, message_id=1, emoji_key="unicode:\u2705", user_id=REVIEWER
        )
    update = await verdicts.on_reaction(1)
    assert update is not None
    assert update.verdict == "pending"

    async with db.session() as session:
        report = await repos.get_report_by_message_id(session, 1)
        assert report is not None
        assert report.verdict == "pending"
        history = await repos.list_report_verdict_history(session, report.id)
    assert [row.verdict for row in history] == ["approved", "pending"]


async def test_reaction_without_a_known_rank_is_kept_but_marked_unknown(db: Database) -> None:
    """An unresolvable reactor must never be treated as a verified reviewer.

    If a NULL capability counted as a reviewer, any stranger's check mark would approve a
    report. But the reaction is still evidence that someone approved, which matters when
    the approver has simply lost the role since, so it is kept in its own category.
    """
    binding_id, _ = await setup_binding(db)
    await add_report(
        db,
        binding_id=binding_id,
        message_id=1,
        author_id=AUTHOR,
        created_at=PERIOD.start.replace(hour=10),
    )
    await add_reaction(
        db, message_id=1, user_id=REVIEWER, emoji="\u2705", ordinal=None, can_review=None
    )

    update = await VerdictService(db).on_reaction(1)
    assert update is not None
    assert update.verdict == "approved_unknown"
    assert update.decided_by_user_id is None, "nobody verified it, so nobody is named"


async def test_a_conflict_counts_as_declined_and_says_nothing(db: Database) -> None:
    """A same-tier disagreement must not announce anything; it simply does not count.

    An automatic weekly run has to finish without producing channel noise, so the report
    is recorded as a conflict, logged, and left out of the credited total.
    """
    from dsbot.modules.quotas.counting import CountedReport, evaluate_reports, tally
    from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
    from dsbot.modules.reports.schema import FormSchemaDef
    from dsbot.modules.reports.yaml_form import parse_form_file
    from dsbot.storage import admin, ranks_repo
    from dsbot.storage import repos as repos_mod

    schema: FormSchemaDef = parse_form_file(Path("forms/training.yaml"))
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, [schema])
        binding = await admin.bind_channel(
            session,
            guild_id=REPORT_GUILD,
            channel_id=CHANNEL,
            module_key="reports",
            schema_key=schema.key,
            rank_source_guild_id=REPORT_GUILD,
        )
        await ranks_repo.replace_ladder(
            session,
            LadderSpec(
                key="review",
                guild_id=REPORT_GUILD,
                purpose="review",
                tiers=[
                    TierSpec(
                        key="senior",
                        label="Senior",
                        ordinal=30,
                        roles=[20],
                        capabilities=["review"],
                    )
                ],
            ),
        )
        report = await repos_mod.upsert_report(
            session,
            channel_id=CHANNEL,
            message_id=1,
            guild_id=REPORT_GUILD,
            author_user_id=AUTHOR,
            created_at=PERIOD.start,
            binding_id=binding.id,
        )
        report.status = "valid"
        report_id = report.id

        # Two reviewers of the SAME tier disagree.
        for user_id in (501, 502):
            await repos_mod.add_reaction(
                session,
                message_id=1,
                channel_id=CHANNEL,
                guild_id=REPORT_GUILD,
                emoji_key="unicode:\u2705" if user_id == 501 else "unicode:\u274c",
                emoji_id=None,
                emoji_name="\u2705" if user_id == 501 else "\u274c",
                user_id=user_id,
                tier_ordinal=30,
                tier_label="Senior",
                can_review=True,
            )

    # The verdict service must not raise or post; it has no reply sink at all now.
    update = await VerdictService(db).on_reaction(1)
    assert update is not None
    assert update.verdict == "conflict"

    async with db.session() as session:
        rows = await repos_mod.list_reactions(session, 1)
    from dsbot.modules.verdict.engine import Rank, Reaction

    reactions = tuple(Reaction(user_id=r.user_id, emoji_name=r.emoji_name) for r in rows)
    ranks = {r.user_id: Rank(user_id=r.user_id, ordinal=30, tier_label="Senior") for r in rows}
    outcomes = evaluate_reports(
        [
            CountedReport(
                report_id=report_id,
                author_user_id=AUTHOR,
                created_at=PERIOD.start,
                reactions=reactions,
                ranks=ranks,
            )
        ]
    )
    summary = tally(outcomes, [])
    assert summary.credited_total() == 0, "a conflict must not credit anybody"

    credited = [t for t in summary.tallies + summary.unranked if t.credited_total]
    assert credited == []

    _, ladder_id = await setup_binding(db)

    class Member:
        def __init__(self, user_id: int, roles: list[int], name: str = "") -> None:
            self.id = user_id
            self.roles = [type("Role", (), {"id": r})() for r in roles]
            self.display_name = name or f"user{user_id}"

    source = StaticRosterSource(
        {
            REPORT_GUILD: [
                Member(AUTHOR, [SENIOR_ROLE], "Ann"),
                Member(700, [], "NoRoles"),
                Member(701, [999], "UnknownRole"),
            ]
        }
    )
    ladder = guild_ladders(ladder_id)
    # This test needs real roles, so the set carries the review tier as well.
    ladder.indexes[0].by_role.update(
        {
            SENIOR_ROLE: ("senior", "Senior Instructor", 30, frozenset({"review"})),
            30: ("instructor", "Instructor", 10, frozenset()),
        }
    )
    service = RosterService(db, source)
    result = await service.refresh(REPORT_GUILD, ladder, ladder_id=ladder_id)

    assert result.ok
    assert [row["user_id"] for row in result.members] == [AUTHOR]
    stored = await service.load(ladder_id)
    assert len(stored) == 1
    assert stored[0]["tier_label"] == "Senior Instructor"


async def test_roster_scrape_without_intent_reports_why(db: Database) -> None:
    _, ladder_id = await setup_binding(db)
    ladder = guild_ladders(ladder_id)
    service = RosterService(db, StaticRosterSource(available=False))
    result = await service.refresh(REPORT_GUILD, ladder, ladder_id=ladder_id)
    assert not result.ok
    assert result.error is not None
    assert "Members intent" in result.error


async def test_count_without_a_roster_is_reported_not_silently_empty(db: Database) -> None:
    binding_id, ladder_id = await setup_binding(db)
    outcome = await CountingService(db).count(
        CountRequest(binding_id=binding_id, channel_id=CHANNEL, period=PERIOD),
        quota_ladder_id=ladder_id,
    )
    assert outcome.degraded
    assert any("roster" in problem for problem in outcome.problems)


async def test_re_running_a_period_replaces_results(db: Database) -> None:
    binding_id, ladder_id = await setup_binding(db)
    async with db.write_session() as session:
        await repos.replace_roster(
            session,
            ladder_id,
            [{"user_id": AUTHOR, "display_name": "Ann", "tier_ordinal": 30}],
        )
    await add_report(
        db,
        binding_id=binding_id,
        message_id=1,
        author_id=AUTHOR,
        created_at=PERIOD.start.replace(hour=10),
    )

    service = CountingService(db)
    request = CountRequest(binding_id=binding_id, channel_id=CHANNEL, period=PERIOD)
    first = await service.count(request, quota_ladder_id=ladder_id, persist=True)
    second = await service.count(request, quota_ladder_id=ladder_id, persist=True)

    assert first.run_id is not None
    assert second.run_id == first.run_id

    async with db.session() as session:
        results = await repos.list_run_results(session, int(first.run_id))
    assert len(results) == 1
    assert results[0].pending_count == 1


async def test_republishing_edits_the_same_messages(db: Database) -> None:
    """A second publish must update in place, not add a duplicate report to the channel."""
    from dsbot.discord.reply import RecordingReplySink

    binding_id, ladder_id = await setup_binding(db)
    async with db.write_session() as session:
        await repos.replace_roster(
            session,
            ladder_id,
            [{"user_id": AUTHOR, "display_name": "Ann", "tier_ordinal": 30}],
        )

    reply = RecordingReplySink()
    service = CountingService(db, reply)
    request = CountRequest(binding_id=binding_id, channel_id=CHANNEL, period=PERIOD)
    outcome = await service.count(request, quota_ladder_id=ladder_id, persist=True)

    first = await service.publish(request, outcome, target_channel_id=CHANNEL)
    second = await service.publish(request, outcome, target_channel_id=CHANNEL)

    assert first == second
    # One send, then an edit: no second copy of the report.
    assert len(reply.sent) == 1
    assert len(reply.edited) == 1


async def test_roster_scrape_keeps_only_people_on_the_ladder(db: Database) -> None:
    _, ladder_id = await setup_binding(db)

    class Member:
        def __init__(self, user_id: int, roles: list[int], name: str = "") -> None:
            self.id = user_id
            self.roles = [type("Role", (), {"id": r})() for r in roles]
            self.display_name = name or f"user{user_id}"

    source = StaticRosterSource(
        {
            REPORT_GUILD: [
                Member(AUTHOR, [SENIOR_ROLE], "Ann"),
                Member(700, [], "NoRoles"),
                Member(701, [999], "UnknownRole"),
            ]
        }
    )
    ladder = guild_ladders(ladder_id)
    ladder.indexes[0].by_role.update(
        {
            SENIOR_ROLE: ("senior", "Senior Instructor", 30, frozenset({"review"})),
            30: ("instructor", "Instructor", 10, frozenset()),
        }
    )
    service = RosterService(db, source)
    result = await service.refresh(REPORT_GUILD, ladder, ladder_id=ladder_id)

    assert result.ok
    assert [row["user_id"] for row in result.members] == [AUTHOR]

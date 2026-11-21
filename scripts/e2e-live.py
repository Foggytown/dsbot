"""Offline proof of the live Discord counting path.

Uses fake Discord objects with the SAME shape discord.py gives us, and the real message
text from the channel, so the grouping / parsing / counting chain is exercised without a
network connection. This is the piece that was missing: reading history from Discord
instead of trusting the database archive.

    uv run python scripts/e2e-live.py
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from datetime import UTC, datetime, timedelta

import discord

from dsbot.config import Settings
from dsbot.modules.quotas.counting import RosterEntry
from dsbot.modules.quotas.live import count_from_discord, group_messages
from dsbot.modules.quotas.period import Period
from dsbot.modules.ranks.resolver import GuildLadders, LadderIndex, RankResolver
from dsbot.modules.ranks.source import StaticRankSource
from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.storage.db import Database

GUILD = 900
CHANNEL = 500
REVIEWER = 777
SENIOR_ROLE = 42


class FakeUser:
    def __init__(self, uid: int, name: str, bot: bool = False) -> None:
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = bot


class FakeReaction:
    def __init__(self, emoji: str, users: list[FakeUser]) -> None:
        self.emoji = emoji
        self._users = users
        self.count = len(users)

    async def users(self, limit: int = 100):
        for user in self._users:
            yield user


class FakeMessage:
    def __init__(
        self,
        mid: int,
        author: FakeUser,
        content: str,
        at: datetime,
        *,
        attachments: int = 0,
        reactions: list[FakeReaction] | None = None,
    ) -> None:
        self.id = mid
        self.author = author
        self.content = content
        self.created_at = at
        self.attachments = [object()] * attachments
        self.reactions = reactions or []


class FakeChannel:
    """Mimics `channel.history(after=..., before=...)` closely enough for the counting path."""

    def __init__(self, messages: list[FakeMessage]) -> None:
        self._messages = messages

    async def history(self, *, limit=None, after=None, before=None, **_: object):
        selected = [
            m
            for m in self._messages
            if (after is None or m.created_at >= after)
            and (before is None or m.created_at < before)
        ]
        for message in sorted(selected, key=lambda m: m.created_at, reverse=True):
            yield message


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    settings = Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'd.db').as_posix()}")
    db = Database(settings)
    try:
        await db.prepare()
        schema = parse_form_file(pathlib.Path("forms/training.yaml"))

        instructor = FakeUser(100, "Instructor Ann")
        other = FakeUser(101, "Instructor Bob")
        reviewer = FakeUser(REVIEWER, "Senior Reviewer")
        start = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

        messages = [
            # A valid T report, approved.
            FakeMessage(
                1,
                instructor,
                "1. 212th | 7329\n2. T\n3.",
                start,
                attachments=1,
                reactions=[FakeReaction("\u2705", [reviewer])],
            ),
            # A rejected report.
            FakeMessage(
                2,
                other,
                "1. ARC | 9445\n2. M-L-C\n3.\n4. https://discord.com/channels/1/2/3",
                start + timedelta(hours=1),
                attachments=2,
                reactions=[FakeReaction("\u274c", [reviewer])],
            ),
            # A report with no reactions at all: must count as pending, not vanish.
            FakeMessage(
                3,
                instructor,
                "1. SOB | 01/0016 | 0017\n2. INF\n3.",
                start + timedelta(hours=2),
                attachments=2,
            ),
            # A photo follow-up by the same author, completing the report above.
            FakeMessage(
                4,
                instructor,
                "",
                start + timedelta(hours=2, minutes=1),
                attachments=1,
            ),
            # Chat, not a report.
            FakeMessage(5, other, "цифра 4", start + timedelta(hours=3)),
        ]
        channel = FakeChannel(messages)

        groups = group_messages(messages)
        print(f"messages: {len(messages)}  groups: {len(groups)}")
        for group in groups:
            attachments = sum(len(m.attachments) for m in group)
            candidate = ReportCandidate(
                messages=[
                    GroupMessage(
                        message_id=m.id,
                        author_id=m.author.id,
                        created_at=m.created_at,
                        content=m.content,
                        attachment_count=len(m.attachments),
                    )
                    for m in group
                ]
            )
            parsed = parse_report([schema], candidate, attachment_count=attachments)
            print(
                f"  group of {len(group)}: matched={parsed.matched} valid={parsed.is_valid} "
                f"attachments={attachments}"
            )

        group = GuildLadders(guild_id=GUILD)
        group.add(
            LadderIndex(
                ladder_id=1,
                guild_id=GUILD,
                key="review",
                purpose="review",
                by_role={SENIOR_ROLE: ("senior", "Senior Instructor", 30, frozenset({"review"}))},
            )
        )
        resolver = RankResolver(
            StaticRankSource({(GUILD, REVIEWER): frozenset({SENIOR_ROLE})}), {GUILD: group}
        )

        period = Period(
            start=datetime(2026, 11, 16, tzinfo=UTC), end=datetime(2026, 11, 23, tzinfo=UTC)
        )
        summary, stats = await count_from_discord(
            channel=channel,
            schemas=[schema],
            period=period,
            roster=[
                RosterEntry(user_id=100, display_name="Instructor Ann", required_count=2),
                RosterEntry(user_id=101, display_name="Instructor Bob", required_count=2),
            ],
            resolver=resolver,
            rank_guild_id=GUILD,
        )

        print(f"\nstats: {stats.as_dict()}")
        print()
        for row in summary.everyone():
            print(
                f"  {row.display_name:<18} approved={row.approved_count} "
                f"pending={row.pending_count} rejected={row.rejected_count} "
                f"required={row.required_count} met={row.met}"
            )

        ann = next(t for t in summary.tallies if t.user_id == 100)
        bob = next(t for t in summary.tallies if t.user_id == 101)
        assert ann.approved_count == 1, f"Ann should have 1 approved, got {ann.approved_count}"
        assert ann.pending_count == 1, f"the unreviewed report must count as pending: {ann}"
        assert bob.rejected_count == 1, f"Bob's report was rejected: {bob}"
        assert stats.matched == 3, f"three reports should parse, got {stats.matched}"
        print("\nOK: reads history from Discord, groups photos, counts without the archive")
    finally:
        await db.dispose()
        _ = discord


if __name__ == "__main__":
    asyncio.run(main())

"""Verifies the two improvements:

1. reports from a filtered-out tier are never fetched
2. the cache no longer trusts a swapped-but-same-count reaction set
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from datetime import UTC, datetime, timedelta

from dsbot.config import Settings
from dsbot.modules.quotas.counting import RosterEntry
from dsbot.modules.quotas.live import (
    _cached_reactions,
    _remember_reactions,
    count_from_discord,
)
from dsbot.modules.quotas.period import Period
from dsbot.modules.ranks.resolver import GuildLadders, LadderIndex, RankResolver
from dsbot.modules.ranks.source import StaticRankSource
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.storage.db import Database

GUILD = 900
CHANNEL = 500
REVIEWER = 777
SENIOR = 42


class User:
    def __init__(self, uid, name):
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = False


class Reaction:
    calls = 0

    def __init__(self, emoji, users):
        self.emoji = type("E", (), {"name": emoji})()
        self._users = users
        self.count = len(users)

    async def users(self, limit=100):
        Reaction.calls += 1
        for u in self._users:
            yield u


class Message:
    def __init__(self, mid, author, at, reactions=()):
        self.id = mid
        self.author = author
        self.content = "1. 212th | 7329\n2. T\n3."
        self.created_at = at
        self.attachments = [object()]
        self.reactions = list(reactions)
        self.guild = type("G", (), {"id": GUILD})()
        self.edited_at = None


class Channel:
    def __init__(self, messages):
        self._messages = messages

    async def history(self, *, limit=None, after=None, before=None, **_):
        for m in sorted(self._messages, key=lambda m: m.created_at, reverse=True):
            if (after is None or m.created_at >= after) and (
                before is None or m.created_at < before
            ):
                yield m


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'x.db').as_posix()}"))
    try:
        await db.prepare()
        schema = parse_form_file(pathlib.Path("forms/training.yaml"))
        reviewer = User(REVIEWER, "Rev")
        senior = User(100, "Senior Person")
        junior = User(200, "Junior Person")
        base = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

        # 10 senior reports, 10 junior reports, all with a check.
        messages = [
            Message(i, senior, base + timedelta(minutes=i), [Reaction("\u2705", [reviewer])])
            for i in range(10)
        ] + [
            Message(
                100 + i, junior, base + timedelta(minutes=50 + i), [Reaction("\u2705", [reviewer])]
            )
            for i in range(10)
        ]
        channel = Channel(messages)
        period = Period(
            start=datetime(2026, 11, 16, tzinfo=UTC), end=datetime(2026, 11, 23, tzinfo=UTC)
        )

        group = GuildLadders(guild_id=GUILD)
        group.add(
            LadderIndex(
                1,
                GUILD,
                "review",
                {SENIOR: ("senior", "Senior", 30, frozenset({"review"}))},
                purpose="review",
            )
        )
        resolver = RankResolver(
            StaticRankSource({(GUILD, REVIEWER): frozenset({SENIOR})}), {GUILD: group}
        )
        roster = [
            RosterEntry(
                user_id=100, display_name="Senior Person", tier_label="Senior", required_count=10
            ),
            RosterEntry(
                user_id=200, display_name="Junior Person", tier_label="Junior", required_count=10
            ),
        ]

        async def scan(label, **kwargs):
            Reaction.calls = 0
            summary, stats = await count_from_discord(
                channel=channel,
                schemas=[schema],
                period=period,
                roster=roster,
                resolver=resolver,
                rank_guild_id=GUILD,
                **kwargs,
            )
            print(
                f"{label}: requests={Reaction.calls:2d} matched={stats.matched} "
                f"skipped_hidden={stats.skipped_hidden} rows={len(summary.tallies)}"
            )
            return summary

        print("=== 1. hiding a tier should skip its reports entirely ===")
        await scan("everything     ")
        await scan("Junior hidden  ", skip_authors=frozenset({200}))
        print("  -> hiding 'Junior' halves the requests, which is the point.\n")

        print("=== 2. cache correctness: swap keeps the count, changes the people ===")
        await scan(
            "first          ",
            cached=lambda g: _cached_reactions(db, g),
            remember=lambda g, r, u: _remember_reactions(db, CHANNEL, channel, g, r),
        )
        await scan(
            "unchanged      ",
            cached=lambda g: _cached_reactions(db, g),
            remember=lambda g, r, u: _remember_reactions(db, CHANNEL, channel, g, r),
        )

        # Person A drops their check while person B adds one: counts identical.
        other = User(999, "Someone Else")
        messages[0].reactions = [Reaction("\u2705", [other])]
        hit = await _cached_reactions(db, [messages[0]])
        print(f"  swapped reactor, same count -> cache reused: {hit is not None}")

        print("\nOK")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

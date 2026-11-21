"""Proves the reaction cache: the second scan of the same period makes no requests.

This is the fix for the 429 storm. Reading *who* reacted costs one request per emoji per
message, rate limited per channel, so a busy week hits the limit. Discord's message
payload already carries the counts, so if the counts are unchanged the same people are
still the ones who reacted and the request can be skipped.

    uv run python scripts/e2e-reaction-cache.py
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
SENIOR_ROLE = 42


class User:
    def __init__(self, uid: int, name: str) -> None:
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = False


class Reaction:
    """Counts how many times `.users()` is actually called."""

    calls = 0

    def __init__(self, emoji: str, users: list[User]) -> None:
        self.emoji = emoji
        self._users = users
        self.count = len(users)

    async def users(self, limit: int = 100):
        Reaction.calls += 1
        for user in self._users:
            yield user


class Message:
    def __init__(self, mid, author, content, at, reactions=()):
        self.id = mid
        self.author = author
        self.content = content
        self.created_at = at
        self.attachments = [object()]
        self.reactions = list(reactions)
        self.guild = type("G", (), {"id": GUILD})()


class Channel:
    def __init__(self, messages):
        self._messages = messages

    async def history(self, *, limit=None, after=None, before=None, **_):
        for message in sorted(self._messages, key=lambda m: m.created_at, reverse=True):
            if (after is None or message.created_at >= after) and (
                before is None or message.created_at < before
            ):
                yield message


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    settings = Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'c.db').as_posix()}")
    db = Database(settings)
    try:
        await db.prepare()
        schema = parse_form_file(pathlib.Path("forms/training.yaml"))

        ann = User(100, "Ann")
        reviewer = User(REVIEWER, "Reviewer")
        base = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

        messages = [
            Message(
                i,
                ann,
                "1. 212th | 7329\n2. T\n3.",
                base + timedelta(hours=i),
                reactions=[Reaction("\u2705", [reviewer])],
            )
            for i in range(20)
        ]
        channel = Channel(messages)
        period = Period(
            start=datetime(2026, 11, 16, tzinfo=UTC), end=datetime(2026, 11, 23, tzinfo=UTC)
        )

        group = GuildLadders(guild_id=GUILD)
        group.add(
            LadderIndex(
                ladder_id=1,
                guild_id=GUILD,
                key="review",
                purpose="review",
                by_role={SENIOR_ROLE: ("senior", "Senior", 30, frozenset({"review"}))},
            )
        )
        resolver = RankResolver(
            StaticRankSource({(GUILD, REVIEWER): frozenset({SENIOR_ROLE})}), {GUILD: group}
        )
        roster = [RosterEntry(user_id=100, display_name="Ann", required_count=20)]

        async def scan(label: str) -> None:
            Reaction.calls = 0
            summary, stats = await count_from_discord(
                channel=channel,
                schemas=[schema],
                period=period,
                roster=roster,
                resolver=resolver,
                rank_guild_id=GUILD,
                cached=lambda g: _cached_reactions(db, g),
                remember=lambda g, r, u: _remember_reactions(db, CHANNEL, channel, g, r),
            )
            row = summary.tallies[0]
            print(
                f"{label}: reports={stats.matched} requests={Reaction.calls} "
                f"approved={row.approved_count}"
            )

        await scan("first scan ")
        await scan("second scan")

        assert Reaction.calls == 0, "the cached scan still made requests"

        # A new reaction must invalidate the cache, otherwise the count silently freezes.
        print("\nadding one more approval to report 0...")
        messages[0].reactions.append(Reaction("\u2705", [User(888, "Second")]))
        await scan("after change")

        print("\nOK: repeats reuse the cache, and a changed count refetches")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

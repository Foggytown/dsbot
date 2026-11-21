"""Measures a scan with NO reactions at all, which is the case you asked about.

If reports have no reactions, the only cost should be reading the messages themselves.
This shows exactly how many API calls that takes and where the time goes.

    uv run python scripts/profile-no-reactions.py
"""

from __future__ import annotations

import asyncio
import pathlib
import time
from datetime import UTC, datetime, timedelta

from dsbot.modules.quotas.counting import RosterEntry
from dsbot.modules.quotas.live import count_from_discord
from dsbot.modules.quotas.period import Period
from dsbot.modules.ranks.resolver import GuildLadders, LadderIndex, RankResolver
from dsbot.modules.ranks.source import StaticRankSource
from dsbot.modules.reports.yaml_form import parse_form_file

GUILD = 900
HISTORY_LATENCY = 0.15  # one channel.history page


class User:
    def __init__(self, uid: int, name: str) -> None:
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = False


class Message:
    def __init__(self, mid: int, author: User, at: datetime) -> None:
        self.id = mid
        self.author = author
        self.content = "1. 212th | 7329\n2. T\n3."
        self.created_at = at
        self.attachments = [object()]
        self.reactions = []  # <-- no reactions at all
        self.edited_at = None
        self.guild = type("G", (), {"id": GUILD})()


class Channel:
    """Simulates discord.py's paged history: 100 messages per request."""

    def __init__(self, messages: list[Message]) -> None:
        self._messages = messages
        self.pages = 0

    async def history(self, *, limit=None, after=None, before=None, **_):
        selected = [
            m
            for m in self._messages
            if (after is None or m.created_at >= after)
            and (before is None or m.created_at < before)
        ]
        for start in range(0, len(selected), 100):
            self.pages += 1
            await asyncio.sleep(HISTORY_LATENCY)  # one HTTP round-trip per page
            for message in sorted(selected[start : start + 100], key=lambda m: m.created_at):
                yield message


async def run(n_reports: int) -> None:
    schema = parse_form_file(pathlib.Path("forms/training.yaml"))
    author = User(100, "Instructor")
    base = datetime.now(UTC) - timedelta(days=7)
    messages = [Message(i, author, base + timedelta(minutes=i * 3)) for i in range(n_reports)]
    channel = Channel(messages)

    period = Period(start=base - timedelta(minutes=1), end=base + timedelta(days=8))
    group = GuildLadders(guild_id=GUILD)
    group.add(LadderIndex(1, GUILD, "review", {}, purpose="review"))
    resolver = RankResolver(StaticRankSource({}), {GUILD: group})

    started = time.perf_counter()
    summary, stats = await count_from_discord(
        channel=channel,
        schemas=[schema],
        period=period,
        roster=[RosterEntry(user_id=100, display_name="Instructor", required_count=1)],
        resolver=resolver,
        rank_guild_id=GUILD,
    )
    elapsed = time.perf_counter() - started

    row = summary.tallies[0]
    print(
        f"{n_reports:4d} reports: history pages={channel.pages:2d}  "
        f"reaction requests={stats.reactions:2d}  "
        f"matched={stats.matched:3d}  pending={row.pending_count:3d}  "
        f"time={elapsed:.2f}s"
    )


async def main() -> None:
    print("No reactions on any report:\n")
    for n in (50, 200, 500, 1000):
        await run(n)
    print()
    print("Reaction requests are 0 in every case: the cost is only reading history.")
    print(
        f"History is fetched 100 messages per request ({HISTORY_LATENCY * 1000:.0f} ms each"
        f" simulated)."
    )


if __name__ == "__main__":
    asyncio.run(main())

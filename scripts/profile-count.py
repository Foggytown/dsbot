"""Times each phase of a live count, so optimisation targets the real cost.

Run with: uv run python scripts/profile-count.py
"""

from __future__ import annotations

import asyncio
import pathlib
import time
from datetime import UTC, datetime, timedelta

from dsbot.modules.quotas.live import _read_reactions, group_messages
from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
from dsbot.modules.reports.yaml_form import parse_form_file

N_REPORTS = 200
N_REACTIONS_PER_REPORT = 2
REST_LATENCY = 0.12  # seconds; typical Discord round-trip


class User:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.name = f"u{uid}"
        self.display_name = f"u{uid}"
        self.bot = False


class Reaction:
    """Counts its `users()` calls and simulates the REST latency of each one."""

    def __init__(self, emoji: str, users: list[User], sink: list[float]) -> None:
        self.emoji = emoji
        self._users = users
        self.count = len(users)
        self._sink = sink

    async def users(self, limit: int = 100):
        started = time.perf_counter()
        await asyncio.sleep(REST_LATENCY)  # simulate the HTTP round-trip
        self._sink.append(time.perf_counter() - started)
        for user in self._users:
            yield user


class Message:
    def __init__(self, mid: int, author: User, content: str, at: datetime, reactions):
        self.id = mid
        self.author = author
        self.content = content
        self.created_at = at
        self.attachments = [object()]
        self.reactions = reactions


async def main() -> None:
    schema = parse_form_file(pathlib.Path("forms/training.yaml"))
    base = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    authors = [User(i) for i in range(1, 21)]
    reviewers = [User(900 + i) for i in range(3)]
    rest_times: list[float] = []

    messages = []
    for i in range(N_REPORTS):
        reactions = [
            Reaction("\u2705", [reviewers[i % 3]], rest_times)
            for _ in range(N_REACTIONS_PER_REPORT)
        ]
        messages.append(
            Message(
                i,
                authors[i % len(authors)],
                "1. ARC | 0001\n2. T\n3.",
                base + timedelta(minutes=i * 3),
                reactions,
            )
        )

    t0 = time.perf_counter()
    groups = group_messages(messages)
    t_group = time.perf_counter() - t0

    t0 = time.perf_counter()
    parsed_matched = 0
    for group in groups:
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
        out = parse_report([schema], candidate, attachment_count=1)
        parsed_matched += int(out.matched)
    t_parse = time.perf_counter() - t0

    t0 = time.perf_counter()
    for group in groups:
        await _read_reactions(group)
    t_reactions = time.perf_counter() - t0

    total = t_group + t_parse + t_reactions
    print(f"messages:            {len(messages)}")
    print(f"groups:              {len(groups)}   ({t_group * 1000:.1f} ms)")
    print(f"parsing:             {parsed_matched} matched   ({t_parse * 1000:.1f} ms)")
    print(f"reading reactions:   {len(rest_times)} REST calls   ({t_reactions:.2f} s)")
    print(f"TOTAL:               {total:.2f} s")
    print()
    print(f"share of time in reaction REST: {t_reactions / total * 100:.0f}%")
    print(f"rest calls per report:          {len(rest_times) / N_REPORTS:.1f}")


if __name__ == "__main__":
    asyncio.run(main())

"""Regressions found while auditing phase 3.

Each test here failed or would have failed before the fix, and describes a real
user-visible wrong answer rather than an internal detail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.core.bus import EventBus
from dsbot.core.events import EmojiRef, ReactionAdded
from dsbot.ingest import Ingestor
from dsbot.storage.db import Database


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    database = Database(settings)
    await database.cat if False else await database.create_all()
    yield database
    await database.dispose()


def reaction(user_id: int = 99, message_id: int = 1) -> ReactionAdded:
    return ReactionAdded(
        channel_id=10,
        guild_id=1,
        message_id=message_id,
        user_id=user_id,
        emoji=EmojiRef(id=None, name="\u2705"),
        reacted_at=datetime(2026, 11, 18, 12, tzinfo=UTC),
    )


async def test_removing_then_re_adding_the_same_reaction_is_processed_again(
    db: Database,
) -> None:
    """Un-react then re-react is a common correction, not a duplicate delivery.

    The event id must therefore be unique per occurrence. If it is not, the second
    add is dropped as a duplicate and the verdict never comes back: the report stays
    undecided forever even though the reaction is visible in Discord.
    """
    bus = EventBus()
    ingestor = Ingestor(db, bus)
    seen: list[str] = []

    async def record(event: ReactionAdded) -> None:
        seen.append(event.external_id)

    bus.subscribe("reaction_added", record)

    assert await ingestor.handle(reaction()) is True
    assert await ingestor.handle(reaction()) is False  # same event twice: a duplicate

    # Now the user removes and re-adds it, a genuinely new occurrence.
    again = reaction()
    again = ReactionAdded(
        channel_id=again.channel_id,
        guild_id=again.guild_id,
        message_id=again.message_id,
        user_id=again.user_id,
        emoji=again.emoji,
        reacted_at=datetime(2026, 11, 18, 12, 30, tzinfo=UTC),
    )
    assert await ingestor.handle(again) is True, "a re-added reaction must be processed"
    await bus.flush()
    assert len(seen) == 2
    await bus.aclose()


async def test_zero_reports_without_a_quota_is_not_reported_as_meeting_it(
    db: Database,
) -> None:
    """`0 of 0` must not be rendered as a passed quota: there is no quota to pass."""
    from dsbot.modules.quotas.counting import CountSummary, MemberTally
    from dsbot.modules.quotas.render import render_csv

    row = MemberTally(user_id=1, display_name="Ann")
    text = render_csv(CountSummary(tallies=[row]))
    assert "no-quota" in text

    met_row = MemberTally(user_id=2, display_name="Bob", approved_count=3, required_count=3)
    short_row = MemberTally(user_id=3, display_name="Cid", approved_count=1, required_count=3)
    text = render_csv(CountSummary(tallies=[met_row, short_row]))
    assert "yes" in text and "no" in text


async def test_clearing_reactions_twice_is_processed_twice(db: Database) -> None:
    """Cleared, re-added, cleared again must not leave stale reactions behind.

    Stale reactions would be counted as approvals, so a dropped clear is a wrong number
    rather than a cosmetic problem.
    """
    from dsbot.core.events import ReactionCleared

    bus = EventBus()
    ingestor = Ingestor(db, bus)

    def clear(at: datetime) -> ReactionCleared:
        return ReactionCleared(
            channel_id=10,
            guild_id=1,
            message_id=1,
            emoji=EmojiRef(id=None, name="\u2705"),
            cleared_at=at,
        )

    first = clear(datetime(2026, 11, 18, 12, tzinfo=UTC))
    second = clear(datetime(2026, 11, 18, 13, tzinfo=UTC))

    assert await ingestor.handle(first) is True
    assert await ingestor.handle(first) is False  # genuinely the same event
    assert await ingestor.handle(second) is True, "a later clear must be processed"
    await bus.aclose()

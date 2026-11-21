"""The reaction cache: fetch who reacted once, then reuse it.

Reading reactors costs one request per emoji per message and is rate limited per channel,
which is what produced the 429 storm. Discord's message payload carries the counts, so an
unchanged count means an unchanged set of reactors and the request can be skipped.

The dangerous direction is a *stale* cache: it would freeze the count and silently report
yesterday's verdicts forever. So every test here is about invalidation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.modules.quotas.live import (
    _cached_reactions,
    _has_verdict_reactions,
    _live_counts,
    _remember_reactions,
)
from dsbot.modules.verdict.engine import Reaction
from dsbot.storage import repos
from dsbot.storage.db import Database

GUILD = 900
CHANNEL = 500
MESSAGE = 1234


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(
        Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")  # type: ignore[call-arg]
    )
    await database.prepare()
    yield database
    await database.dispose()


class Emoji:
    def __init__(self, name: str) -> None:
        self.name = name


class ReactionOnMessage:
    def __init__(self, name: str, count: int) -> None:
        self.emoji = Emoji(name)
        self.count = count


class Message:
    def __init__(self, reactions: list[ReactionOnMessage], mid: int = MESSAGE) -> None:
        self.id = mid
        self.reactions = reactions
        self.guild = type("G", (), {"id": GUILD})()


def test_live_counts_reads_only_verdict_emoji() -> None:
    group = [Message([ReactionOnMessage("\u2705", 2), ReactionOnMessage("\U0001f44d", 5)])]
    assert _live_counts(group) == {"unicode:\u2705": 2}


def test_a_message_without_verdict_reactions_needs_no_fetch() -> None:
    assert _has_verdict_reactions([Message([])]) is False
    assert _has_verdict_reactions([Message([ReactionOnMessage("\U0001f44d", 1)])]) is False
    assert _has_verdict_reactions([Message([ReactionOnMessage("\u2705", 1)])]) is True


async def test_a_missing_cache_yields_nothing(db: Database) -> None:
    assert await _cached_reactions(db, [Message([ReactionOnMessage("\u2705", 1)])]) is None


async def test_a_stored_list_is_returned_when_counts_match(db: Database) -> None:
    group = [Message([ReactionOnMessage("\u2705", 1)])]
    await _remember_reactions(db, CHANNEL, None, group, [Reaction(user_id=7, emoji_name="\u2705")])

    hit = await _cached_reactions(db, group)
    assert hit is not None
    reactions, users = hit
    assert [r.user_id for r in reactions] == [7]
    assert users == [7]


async def test_a_changed_count_invalidates_the_cache(db: Database) -> None:
    """The important direction: a new approval must not be served from the cache."""
    group = [Message([ReactionOnMessage("\u2705", 1)])]
    await _remember_reactions(db, CHANNEL, None, group, [Reaction(user_id=7, emoji_name="\u2705")])

    # Somebody else approved since; Discord now reports 2 for that emoji.
    group = [Message([ReactionOnMessage("\u2705", 2)])]
    assert await _cached_reactions(db, group) is None, "a stale count must refetch"


async def test_removing_a_reaction_invalidates_the_cache(db: Database) -> None:
    group = [Message([ReactionOnMessage("\u2705", 2)])]
    await _remember_reactions(
        db,
        CHANNEL,
        None,
        group,
        [Reaction(user_id=7, emoji_name="\u2705"), Reaction(user_id=8, emoji_name="\u2705")],
    )
    group = [Message([ReactionOnMessage("\u2705", 1)])]
    assert await _cached_reactions(db, group) is None


async def test_remembering_replaces_rather_than_appends(db: Database) -> None:
    group = [Message([ReactionOnMessage("\u2705", 1)])]
    await _remember_reactions(db, CHANNEL, None, group, [Reaction(user_id=7, emoji_name="\u2705")])
    await _remember_reactions(db, CHANNEL, None, group, [Reaction(user_id=8, emoji_name="\u2705")])
    async with db.session() as session:
        rows = await repos.list_cached_reactions(session, MESSAGE)
    assert [row.user_id for row in rows] == [8]

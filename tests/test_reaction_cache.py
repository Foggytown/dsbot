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
    def __init__(
        self,
        reactions: list[ReactionOnMessage],
        mid: int = MESSAGE,
        *,
        created_at: object = None,
    ) -> None:
        self.id = mid
        self.reactions = reactions
        self.created_at = created_at
        self.edited_at = None
        self.guild = type("G", (), {"id": GUILD})()


def _message(name: str, count: int, *, created_at: object = None) -> Message:
    return Message([ReactionOnMessage(name, count)], created_at=created_at)


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


async def test_a_swap_on_a_closed_period_is_trusted(db: Database) -> None:
    """A month-old report cannot change any more, so re-running the week is free."""
    from datetime import UTC, datetime, timedelta

    closed = datetime.now(UTC) - timedelta(days=30)
    group = [_message("\u2705", 1, created_at=closed)]
    await _remember_reactions(db, CHANNEL, None, group, [Reaction(user_id=7, emoji_name="\u2705")])
    assert await _cached_reactions(db, group) is not None


async def test_a_recent_message_is_only_trusted_briefly(db: Database) -> None:
    """On a live week a swap is plausible, so the entry must expire fast."""
    from datetime import UTC, datetime

    from dsbot.modules.quotas import live as live_module

    age = live_module.REACTION_CACHE_MAX_AGE * 2
    recent = datetime.now(UTC) - age
    group = [_message("\u2705", 1, created_at=recent)]
    await _remember_reactions(db, CHANNEL, None, group, [Reaction(user_id=7, emoji_name="\u2705")])

    # Backdate the stored rows: the entry was written long ago, the message is recent.
    from sqlalchemy import update

    from dsbot.storage.models import ReactionCacheEntry

    async with db.write_session() as session:
        await session.execute(
            update(ReactionCacheEntry)
            .where(ReactionCacheEntry.message_id == MESSAGE)
            .values(captured_at=datetime.now(UTC) - age)
        )

    hit = await _cached_reactions(db, group)
    assert hit is None, "an old entry on a recent message must be refetched"


async def test_the_cache_never_touches_the_live_reaction_table(db: Database) -> None:
    """A scan must not disturb the gateway's own record.

    The live path stores every reaction with a rank snapshot, which is what decides who
    had authority. Keeping the cache in the same table meant a scan deleted those rows and
    replaced them with empty ones, silently destroying a real approval's provenance.
    """
    from dsbot.storage import repos

    message_id = 4242
    async with db.write_session() as session:
        await repos.add_reaction(
            session,
            message_id=message_id,
            channel_id=CHANNEL,
            guild_id=GUILD,
            emoji_key="unicode:\u2705",
            emoji_id=None,
            emoji_name="\u2705",
            user_id=555,
            tier_key="senior",
            tier_ordinal=30,
            tier_label="Senior Instructor",
            can_review=True,
        )

    # A scan caches a *different* reactor for the same message.
    group = [_message("\u2705", 2, created_at=None)]
    group[0].id = message_id
    await _remember_reactions(
        db, CHANNEL, None, group, [Reaction(user_id=999, emoji_name="\u2705")]
    )

    async with db.session() as session:
        live = await repos.list_reactions(session, message_id)
    assert [row.user_id for row in live] == [555], "the live snapshot was deleted"
    assert live[0].can_review is True
    assert live[0].tier_ordinal == 30, "the rank snapshot was lost"

    """The unreviewed case must stay free: no request, no cache lookup, no DB read.

    This is the guarantee that makes a period where nobody reacted cheap. If a database
    round-trip or an API call creeps in here, a 1000-report week becomes 1000 queries for
    nothing.
    """
    from dsbot.modules.quotas.live import _read_all_reactions

    called = {"cached": 0, "remember": 0}

    async def cached(group: object) -> None:
        called["cached"] += 1
        return None

    async def remember(group: object, reactions: object, reactors: object) -> None:
        called["remember"] += 1

    groups = [[Message([])] for _ in range(5)]
    results = await _read_all_reactions(groups, cached=cached, remember=remember)

    assert results == [([], [])] * 5
    assert called == {"cached": 0, "remember": 0}, "the free path did extra work"


async def test_a_message_without_verdict_reactions_touches_nothing(db: Database) -> None:
    """The unreviewed case must stay free: no request, no cache lookup, no DB read."""
    from dsbot.modules.quotas.live import _read_all_reactions

    called = {"cached": 0, "remember": 0}

    async def cached(group: object) -> None:
        called["cached"] += 1
        return None

    async def remember(group: object, reactions: object, reactors: object) -> None:
        called["remember"] += 1

    groups = [[Message([])] for _ in range(5)]
    results = await _read_all_reactions(groups, cached=cached, remember=remember)

    assert results == [([], [])] * 5
    assert called == {"cached": 0, "remember": 0}, "the free path did extra work"


async def test_a_thumb_only_reaction_is_also_free(db: Database) -> None:
    """A thumbs-up is not a verdict, so it must not trigger a fetch either."""
    from dsbot.modules.quotas.live import _read_all_reactions

    called = {"cached": 0}

    async def cached(group: object) -> None:
        called["cached"] += 1
        return None

    groups = [[Message([ReactionOnMessage("\U0001f44d", 3)])]]
    await _read_all_reactions(groups, cached=cached, remember=None)
    assert called["cached"] == 0


def test_hidden_tiers_skip_their_authors() -> None:
    """Hiding a tier must also skip its reports, not merely hide the rows."""
    from dsbot.modules.quotas.counting import RosterEntry
    from dsbot.modules.quotas.live import _hidden_authors

    roster = [
        RosterEntry(user_id=1, tier_label="Senior"),
        RosterEntry(user_id=2, tier_label="Junior"),
        RosterEntry(user_id=3, tier_label="Исключение"),
    ]
    skipped = _hidden_authors(roster, hidden_tiers=["Junior", "исключение"], only_tier=None)
    assert skipped == frozenset({2, 3})


def test_selecting_one_tier_skips_everyone_else() -> None:
    from dsbot.modules.quotas.counting import RosterEntry
    from dsbot.modules.quotas.live import _hidden_authors

    roster = [
        RosterEntry(user_id=1, tier_label="Senior"),
        RosterEntry(user_id=2, tier_label="Junior"),
    ]
    assert _hidden_authors(roster, hidden_tiers=[], only_tier="Senior") == frozenset({2})

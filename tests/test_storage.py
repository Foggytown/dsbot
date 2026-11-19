"""Database tests: idempotency is the property that matters most.

Backfill and live ingest race by design, so double-writing one event must be a no-op.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.core.bus import EventBus
from dsbot.core.events import EmojiRef, MessageCreated, MessageSnapshot, ReactionAdded
from dsbot.ingest import Ingestor
from dsbot.storage import repos
from dsbot.storage.db import Database


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


def snapshot(message_id: int = 1, channel_id: int = 10) -> MessageSnapshot:
    return MessageSnapshot(
        message_id=message_id,
        channel_id=channel_id,
        guild_id=1,
        author_id=42,
        author_name="instructor",
        author_display_name="Instructor",
        author_is_bot=False,
        content="1. SOB | 0016\n2. INF",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_message_upsert_is_idempotent(db: Database) -> None:
    snap = snapshot()
    async with db.write_session() as session:
        await repos.upsert_message(session, snap)
    async with db.write_session() as session:
        await repos.upsert_message(session, snap)

    async with db.session() as session:
        message = await repos.get_message(session, 10, 1)
    assert message is not None
    assert message.content.startswith("1. SOB")


async def test_message_edit_updates_in_place(db: Database) -> None:
    snap = snapshot()
    async with db.write_session() as session:
        await repos.upsert_message(session, snap)

    edited = dataclasses.replace(snap, content="1. SOB | 0099")
    async with db.write_session() as session:
        await repos.upsert_message(session, edited)

    async with db.session() as session:
        message = await repos.get_message(session, 10, 1)
    assert message is not None
    assert "0099" in message.content


async def test_raw_event_deduplication(db: Database) -> None:
    event = MessageCreated(message=snapshot())
    ingestor = Ingestor(db, EventBus())

    await ingestor.handle(event)
    await ingestor.handle(event)  # same event twice

    async with db.session() as session:
        from sqlalchemy import func, select

        from dsbot.storage.models import RawEvent

        count = await session.scalar(select(func.count(RawEvent.id)))
    assert count == 1


async def test_raw_event_stores_full_payload(db: Database) -> None:
    event = MessageCreated(message=snapshot())
    ingestor = Ingestor(db, EventBus())
    await ingestor.handle(event)

    async with db.session() as session:
        from sqlalchemy import select

        from dsbot.storage.models import RawEvent

        row = await session.scalar(select(RawEvent))
    assert row is not None
    assert row.kind == "message_created"
    assert "1. SOB | 0016" in row.payload["message"]["content"]


async def test_reaction_add_is_idempotent(db: Database) -> None:
    for _ in range(2):
        async with db.write_session() as session:
            await repos.add_reaction(
                session,
                message_id=1,
                channel_id=10,
                guild_id=1,
                emoji_key="unicode:✅",
                emoji_id=None,
                emoji_name="✅",
                user_id=99,
                tier_ordinal=5,
            )

    async with db.session() as session:
        reactions = await repos.list_reactions(session, 1)
    assert len(reactions) == 1
    assert reactions[0].tier_ordinal == 5


async def test_reaction_removal_leaves_nothing(db: Database) -> None:
    async with db.write_session() as session:
        await repos.add_reaction(
            session,
            message_id=1,
            channel_id=10,
            guild_id=1,
            emoji_key="unicode:✅",
            emoji_id=None,
            emoji_name="✅",
            user_id=99,
        )
        await repos.remove_reaction(session, message_id=1, emoji_key="unicode:✅", user_id=99)

    async with db.session() as session:
        assert list(await repos.list_reactions(session, 1)) == []


async def test_rank_snapshot_is_stored_per_reaction(db: Database) -> None:
    """A later promotion must not rewrite the rank recorded at decision time."""
    event = ReactionAdded(
        channel_id=10,
        guild_id=1,
        message_id=1,
        user_id=99,
        emoji=EmojiRef(id=None, name="✅"),
        reacted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert event.emoji.key == "unicode:✅"

    async with db.write_session() as session:
        await repos.add_reaction(
            session,
            message_id=1,
            channel_id=10,
            guild_id=1,
            emoji_key=event.emoji.key,
            emoji_id=None,
            emoji_name="✅",
            user_id=99,
            tier_ordinal=7,
            tier_label="Head Instructor",
        )

    async with db.session() as session:
        reactions = await repos.list_reactions(session, 1)
    assert reactions[0].tier_label == "Head Instructor"
    assert reactions[0].tier_ordinal == 7


async def test_binding_roundtrip(db: Database) -> None:
    from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
    from dsbot.storage import admin

    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
        binding = await admin.bind_channel(
            session,
            guild_id=1,
            channel_id=10,
            module_key="reports",
            schema_key="training_report",
            rank_source_guild_id=500,
        )
        assert binding.id is not None

    async with db.session() as session:
        found = await repos.get_binding_by_channel(session, 10)
    assert found is not None
    assert found.rank_source_guild_id == 500
    assert found.module_key == "reports"


async def test_write_lock_serialises_concurrent_writers(db: Database) -> None:
    import asyncio

    async def write(n: int) -> None:
        async with db.write_session() as session:
            await repos.upsert_message(session, snapshot(message_id=n))

    await asyncio.gather(*(write(i) for i in range(20)))

    async with db.session() as session:
        from sqlalchemy import func, select

        from dsbot.storage.models import Message

        count = await session.scalar(select(func.count(Message.id)))
    assert count == 20

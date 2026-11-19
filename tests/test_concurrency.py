"""Tests for the concurrency and formation bugs found in the third live test."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.core.bus import EventBus
from dsbot.core.events import MessageCreated, MessageSnapshot
from dsbot.core.lock import InstanceLock
from dsbot.ingest import Ingestor
from dsbot.modules.reports.fields import parse_formation_idn
from dsbot.storage.db import Database


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'conc.db'}")
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


def snapshot(message_id: int = 1) -> MessageSnapshot:
    return MessageSnapshot(
        message_id=message_id,
        channel_id=10,
        guild_id=1,
        author_id=42,
        author_name="a",
        author_display_name="A",
        author_is_bot=False,
        content="1. SOB | 0016\n2. INF",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# --- duplicate event insert ------------------------------------------------


async def test_duplicate_event_does_not_raise(db: Database) -> None:
    """This used to crash with UNIQUE constraint failed: raw_events.external_id."""
    ingestor = Ingestor(db, EventBus())
    event = MessageCreated(message=snapshot())

    first = await ingestor.handle(event)
    second = await ingestor.handle(event)  # must not raise
    assert first is not False
    assert second is False

    async with db.session() as session:
        from sqlalchemy import func, select

        from dsbot.storage.models import RawEvent

        count = await session.scalar(select(func.count(RawEvent.id)))
    assert count == 1


async def test_concurrent_duplicate_inserts_do_not_crash(db: Database) -> None:
    """Two processes publishing the same event is a real scenario, not a test artifact."""
    import asyncio

    ingestor = Ingestor(db, EventBus())
    event = MessageCreated(message=snapshot())

    results = await asyncio.gather(
        *(ingestor.handle(event) for _ in range(5)), return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, errors

    async with db.session() as session:
        from sqlalchemy import func, select

        from dsbot.storage.models import RawEvent

        count = await session.scalar(select(func.count(RawEvent.id)))
    assert count == 1


async def test_distinct_events_both_stored(db: Database) -> None:
    ingestor = Ingestor(db, EventBus())
    await ingestor.handle(MessageCreated(message=snapshot(1)))
    await ingestor.handle(MessageCreated(message=snapshot(2)))

    async with db.session() as session:
        from sqlalchemy import func, select

        from dsbot.storage.models import RawEvent

        count = await session.scalar(select(func.count(RawEvent.id)))
    assert count == 2


# --- single instance lock --------------------------------------------------


def test_second_instance_is_refused(tmp_path: Path) -> None:
    """Two bots on one database cause duplicated handling and insert races."""
    lock_path = tmp_path / "dsbot.lock"
    first = InstanceLock(lock_path)
    first.acquire()
    try:
        second = InstanceLock(lock_path)
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
    finally:
        first.release()


def test_lock_can_be_reacquired_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "dsbot.lock"
    with InstanceLock(lock_path):
        pass
    with InstanceLock(lock_path):
        pass


# --- formations are finite, IDNs are not -----------------------------------


def test_known_formation_accepted() -> None:
    result = parse_formation_idn("SOB | 0016", "f", ["SOB", "ARC"])
    assert not [i for i in result.issues if i.code == "formation_unknown"]


def test_unknown_formation_rejected() -> None:
    result = parse_formation_idn("ZZZ | 0016", "f", ["SOB", "ARC"])
    issue = next(i for i in result.issues if i.code == "formation_unknown")
    assert issue.severity == "error"


def test_formation_match_is_case_insensitive() -> None:
    result = parse_formation_idn("sob | 0016", "f", ["SOB"])
    assert not [i for i in result.issues if i.code == "formation_unknown"]


def test_idn_is_not_validated_against_any_list() -> None:
    """IDNs are open-ended: any 4-digit (or 6+) value must be accepted."""
    for idn in ("0001", "9999", "123456", "000016"):
        result = parse_formation_idn(f"SOB | {idn}", "f", ["SOB"])
        assert not result.issues, (idn, [i.code for i in result.issues])


def test_reaper_and_666th_are_formations_not_qualifications() -> None:
    result = parse_formation_idn("REAPER | 0016", "f", ["SOB", "REAPER", "666TH"])
    assert not [i for i in result.issues if i.code == "formation_unknown"]

    result = parse_formation_idn("666th | 12345", "f", ["SOB", "REAPER", "666TH"])
    assert not [i for i in result.issues if i.code == "formation_unknown"]
    assert [i.code for i in result.issues] == ["idn_bad_length"]

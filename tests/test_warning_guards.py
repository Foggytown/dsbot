"""Guards that keep the bot from nagging the channel about old or unchanged reports.

These encode a real incident: after a deploy the bot posted warnings about messages from
half a year ago. The log showed five `warning_posted` lines in six seconds in the middle
of the night, with no matching `report_processed` line, because the warnings came from
the edit path rather than from a new message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.core.events import MessageSnapshot
from dsbot.discord.reply import RecordingReplySink
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.modules.reports.service import DEFAULT_WARN_MAX_AGE, ReportService
from dsbot.storage import admin, repos
from dsbot.storage.db import Database

CHANNEL = 600
GUILD = 901
AUTHOR = 43
INCOMPLETE = "1. SOB | 0016\n2. INF\n3."


@pytest.fixture
async def bound_db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    db = Database(settings)
    await db.create_all()
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
        await admin.bind_channel(
            session,
            guild_id=GUILD,
            channel_id=CHANNEL,
            module_key="reports",
            schema_key="training_report",
            locale="ru",
        )
    yield db
    await db.dispose()


async def store(
    db: Database, *, message_id: int, age: timedelta, content: str = INCOMPLETE
) -> None:
    async with db.write_session() as session:
        await repos.upsert_message(
            session,
            MessageSnapshot(
                message_id=message_id,
                channel_id=CHANNEL,
                guild_id=GUILD,
                author_id=AUTHOR,
                author_name="instructor",
                author_display_name="Instr",
                author_is_bot=False,
                content=content,
                created_at=datetime.now(UTC) - age,
            ),
        )


async def test_the_guard_is_on_by_default(bound_db: Database) -> None:
    """A caller that forgets the argument still gets the guard.

    Live bot, replay and backfill all re-evaluate messages they did not receive now, so
    making this opt-in would restore the incident in whichever path forgot it.
    """
    assert timedelta(hours=24) == DEFAULT_WARN_MAX_AGE

    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)  # no warn_max_age passed on purpose
    await store(bound_db, message_id=1, age=timedelta(days=180))

    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert sink.sent == [], "the default must suppress an ancient warning"


async def test_a_six_month_old_report_is_silent(bound_db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store(bound_db, message_id=1, age=timedelta(days=180))

    result = await service.process_message(CHANNEL, AUTHOR, 1)

    assert result is not None
    assert result.matched and not result.valid
    assert result.action == "stored", "an old report must be recorded, not announced"
    assert sink.sent == []


async def test_an_old_report_is_still_stored_for_the_archive(bound_db: Database) -> None:
    """Silence in the channel must not mean silence in the database."""
    service = ReportService(bound_db, RecordingReplySink())
    await store(bound_db, message_id=1, age=timedelta(days=180))

    await service.process_message(CHANNEL, AUTHOR, 1)

    async with bound_db.session() as session:
        report = await repos.get_report_by_message(session, CHANNEL, 1)
    assert report is not None
    assert report.status == "invalid"


async def test_a_fresh_report_is_still_warned(bound_db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store(bound_db, message_id=1, age=timedelta(minutes=5))

    result = await service.process_message(CHANNEL, AUTHOR, 1)

    assert result is not None
    assert result.action == "warned"
    assert len(sink.sent) == 1


async def test_zero_disables_the_guard_for_a_deliberate_backfill(bound_db: Database) -> None:
    """An operator backfilling an unread channel may want warnings at any age."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink, warn_max_age=timedelta(0))
    await store(bound_db, message_id=1, age=timedelta(days=180))

    result = await service.process_message(CHANNEL, AUTHOR, 1)

    assert result is not None
    assert result.action == "warned"


async def test_the_warning_names_the_report_it_is_about(bound_db: Database) -> None:
    """Both ids are recorded, so a log line leads back to the report."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store(bound_db, message_id=4242, age=timedelta(minutes=5))

    result = await service.process_message(CHANNEL, AUTHOR, 4242)
    assert result is not None and result.action == "warned"

    async with bound_db.session() as session:
        record = await repos.get_validation_message(session, result.report_id)
    assert record is not None
    assert record.report_message_id == 4242
    # The warning the bot posted is a different message from the report.
    assert record.message_id == sink.sent_ids[0]
    assert record.message_id != record.report_message_id


# --- phantom MESSAGE_UPDATE: the incident ---------------------------------
#
# Payloads captured from the live server. Ten of them had no `edited_timestamp` while
# still carrying the current content, and each one re-parsed a 2025 report and posted a
# warning about it. A content comparison alone misses them, because the content was
# already what the database held.


class _RawUpdate:
    def __init__(self, data: dict, cached: object = None) -> None:
        self.data = data
        self.cached_message = cached


class _Cached:
    def __init__(self, content: str) -> None:
        self.content = content


def test_a_phantom_update_without_edited_timestamp_is_not_an_edit() -> None:
    from dsbot.bot import _edit_changed_message

    payload = _RawUpdate({"content": "1. SOB | RC-77\n2. T\n3.", "edited_timestamp": None})
    assert _edit_changed_message(payload) is False  # type: ignore[arg-type]


def test_a_real_edit_with_edited_timestamp_is_processed() -> None:
    from dsbot.bot import _edit_changed_message

    payload = _RawUpdate(
        {
            "content": "1. SOB | 01/0016\n2. INF\n3.",
            "edited_timestamp": "2026-11-21T21:47:06.515000+00:00",
        }
    )
    assert _edit_changed_message(payload) is True  # type: ignore[arg-type]


def test_the_guard_reads_the_field_discord_actually_sends() -> None:
    """A guard keyed on the wrong field name silently drops every real edit.

    The wire payload uses `edited_timestamp`; `edited_at` is what *our* MessageSnapshot
    dataclass calls it. Checking `edited_at` on the raw gateway dict always returned None,
    so every genuine edit was treated as a phantom update: no re-parse, no warning update.
    Every test still passed, because the tests were written from the same wrong assumption.

    This asserts against the real shape Discord documents, so the two cannot drift again.
    """
    from dsbot.bot import _edit_changed_message

    # Exactly the keys Discord sends for a user editing a message.
    wire = {
        "id": "1573991757002055723",
        "channel_id": "1274460260941434901",
        "content": "1. SOB | 0016\n2. INF\n3. фото\n4. https://discord.com/channels/1/2/3",
        "edited_timestamp": "2026-11-22T13:00:00.000000+00:00",
        "timestamp": "2026-11-22T12:00:00.000000+00:00",
    }
    assert _edit_changed_message(_RawUpdate(wire)) is True  # type: ignore[arg-type]
    # And a payload with no edited_timestamp is a server-side update, not an edit.
    phantom = {k: v for k, v in wire.items() if k != "edited_timestamp"}
    assert _edit_changed_message(_RawUpdate(phantom)) is False  # type: ignore[arg-type]


def test_an_edit_with_identical_content_is_skipped() -> None:
    """Discord can send edited_timestamp for a no-op save; re-parsing wastes work."""
    from dsbot.bot import _edit_changed_message

    payload = _RawUpdate(
        {"content": "same text", "edited_timestamp": "2026-11-21T21:47:06.515000+00:00"},
        cached=_Cached("same text"),
    )
    assert _edit_changed_message(payload) is False  # type: ignore[arg-type]

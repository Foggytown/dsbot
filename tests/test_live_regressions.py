"""Regression tests for the bugs found during the first live channel test.

Every test here corresponds to something that actually went wrong with real messages:
a photo that never completed its report, a deferral that never resolved, and one photo
satisfying two evidence fields.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.core.bus import EventBus
from dsbot.core.events import AttachmentRef, MessageCreated, MessageSnapshot
from dsbot.discord.reply import RecordingReplySink
from dsbot.ingest import Ingestor
from dsbot.jobs.retry import ImmediateRetryScheduler
from dsbot.modules.reports.parser import GroupMessage, ReportCandidate, parse_report
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.modules.reports.service import ReportService
from dsbot.storage import admin, repos
from dsbot.storage.db import Database

CHANNEL = 700
GUILD = 901
AUTHOR = 55

# Exactly what was posted in the live test.
REAL_REPORT = "1. SOB | 0016 \n2. INF\n3. \n4."
BAD_ENUM = "1. SOB  | 001\n2. pepe\n3. -\n4. -"
CHAT = "hey"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
    database = Database(settings)
    await database.create_all()
    async with database.write_session() as session:
        await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
        await admin.bind_channel(
            session,
            guild_id=GUILD,
            channel_id=CHANNEL,
            module_key="reports",
            schema_key="training_report",
        )
    yield database
    await database.dispose()


async def post(
    db: Database,
    *,
    message_id: int,
    content: str = "",
    attachments: int = 0,
    minutes: int = 0,
    author: int = AUTHOR,
) -> None:
    snapshot = MessageSnapshot(
        message_id=message_id,
        channel_id=CHANNEL,
        guild_id=GUILD,
        author_id=author,
        author_name="tester",
        author_display_name="Tester",
        author_is_bot=False,
        content=content,
        created_at=datetime.now(UTC) + timedelta(minutes=minutes),
        attachments=tuple(
            AttachmentRef(id=500 + i, filename=f"p{i}.png", url=f"https://cdn/{i}.png")
            for i in range(attachments)
        ),
    )
    async with db.write_session() as session:
        await repos.upsert_message(session, snapshot)


def group(content: str, attachments: int) -> ReportCandidate:
    return ReportCandidate(
        messages=[
            GroupMessage(
                message_id=1,
                author_id=AUTHOR,
                created_at=datetime.now(UTC),
                content=content,
                attachment_count=attachments,
            )
        ]
    )


# --- bug 1: one photo satisfied two evidence fields ------------------------


def test_one_photo_does_not_satisfy_two_evidence_fields() -> None:
    result = parse_report(list(DEFAULT_SCHEMAS), group(REAL_REPORT, 1), attachment_count=1)
    assert not result.is_valid
    missing = {i.field_id for i in result.issues if i.code == "field_missing"}
    assert "evidence_2" in missing
    assert "evidence_1" not in missing


def test_two_photos_satisfy_both_evidence_fields() -> None:
    result = parse_report(list(DEFAULT_SCHEMAS), group(REAL_REPORT, 2), attachment_count=2)
    assert result.is_valid, [i.to_payload() for i in result.issues]


# --- bug 2: evidence written as one block ----------------------------------


def test_single_evidence_block_resolves_both_subfields() -> None:
    """ "3. Доказательства:" as one item must feed 3.1 and 3.2, not neither."""
    content = "1. SOB | 0016\n2. INF\n3. Доказательства:"
    result = parse_report(list(DEFAULT_SCHEMAS), group(content, 2), attachment_count=2)
    missing = {i.field_id for i in result.issues if i.code == "field_missing"}
    assert "evidence_1" not in missing
    assert "evidence_2" not in missing


def test_nested_subfield_still_preferred_when_present() -> None:
    content = (
        "1. SOB | 0016\n2. INF\n3. Доказательства:\n"
        "     - 3.1. Фото в инж. блоке.\n     - 3.2. Фото практики."
    )
    result = parse_report(list(DEFAULT_SCHEMAS), group(content, 2), attachment_count=2)
    bindings = {b.spec.id: b for b in result.fields}
    assert bindings["evidence_1"].item_path == (3, 1)
    assert bindings["evidence_2"].item_path == (3, 2)


# --- bug 3: unknown qualification was only a warning -----------------------


def test_unknown_qualification_is_an_error() -> None:
    result = parse_report(list(DEFAULT_SCHEMAS), group(BAD_ENUM, 0))
    errors = [i for i in result.issues if i.code == "enum_unknown_option"]
    assert errors
    assert errors[0].severity == "error"
    assert not result.is_valid


def test_partial_idn_is_still_parsed() -> None:
    result = parse_report(list(DEFAULT_SCHEMAS), group(BAD_ENUM, 0))
    formation = result.field_value("formation_idn")
    assert formation is not None
    assert formation["formation"] == "SOB"
    assert formation["idn_number"] == "0001"


# --- bug 4: a deferral that never resolves --------------------------------


async def test_deferred_report_is_rescheduled(db: Database) -> None:
    retry = ImmediateRetryScheduler()
    service = ReportService(db, RecordingReplySink(), retry)
    await post(db, message_id=1, content=REAL_REPORT)

    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert result.action == "deferred"
    assert len(retry.scheduled) == 1
    assert retry.scheduled[0]["message_id"] == 1


async def test_retry_warns_when_photos_never_arrive(db: Database) -> None:
    """The exact live failure: report deferred, author never posts the photo."""
    sink = RecordingReplySink()
    service = ReportService(db, sink, ImmediateRetryScheduler())
    await post(db, message_id=1, content=REAL_REPORT, minutes=-30)

    result = await service.retry_group(CHANNEL, 1)
    assert result is not None
    assert result.action == "warned"
    assert len(sink.sent) == 1


async def test_retry_clears_nothing_and_stays_silent_when_complete(db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(db, sink, ImmediateRetryScheduler())
    await post(db, message_id=1, content=REAL_REPORT, attachments=2, minutes=-30)

    result = await service.retry_group(CHANNEL, 1)
    assert result is not None
    assert result.valid
    assert sink.sent == []


async def test_grace_window_runs_from_the_report_not_the_latest_message(
    db: Database,
) -> None:
    """Chatter must not postpone the warning forever."""
    service = ReportService(db, RecordingReplySink(), ImmediateRetryScheduler())
    await post(db, message_id=1, content=REAL_REPORT, minutes=-30)
    await post(db, message_id=2, content="", attachments=0, minutes=-1)

    result = await service.retry_group(CHANNEL, 1)
    assert result is not None
    assert result.action == "warned"


# --- bug 5: the photo formed its own orphan group -------------------------


async def test_pending_lookup_finds_the_waiting_report(db: Database) -> None:
    service = ReportService(db, RecordingReplySink(), ImmediateRetryScheduler())
    await post(db, message_id=1, content=REAL_REPORT, minutes=-2)
    # The report must have been processed (deferred) before a photo can complete it.
    first = await service.process_message(CHANNEL, AUTHOR, 1)
    assert first is not None and first.action == "deferred"

    await post(db, message_id=2, content="", attachments=2, minutes=-1)
    pending = await service.pending_for_author(CHANNEL, AUTHOR, 2)
    assert 1 in pending


async def test_photo_completes_the_report_through_pending_path(db: Database) -> None:
    """End-to-end, matching live ordering: report arrives, then photo, then re-check."""
    sink = RecordingReplySink()
    service = ReportService(db, sink, ImmediateRetryScheduler())

    # 1. report arrives alone and is deferred
    await post(db, message_id=1, content=REAL_REPORT, minutes=-2)
    first = await service.process_message(CHANNEL, AUTHOR, 1)
    assert first is not None and first.action == "deferred"

    # 2. photo arrives afterwards
    await post(db, message_id=2, content="", attachments=2, minutes=-1)
    pending = await service.pending_for_author(CHANNEL, AUTHOR, 2)
    assert 1 in pending

    # 3. the bot re-checks the earlier report
    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert result.valid, result.issues


async def test_photo_already_present_makes_the_report_valid_immediately(
    db: Database,
) -> None:
    """If the photo lands before the bot catches up, no deferral is needed."""
    service = ReportService(db, RecordingReplySink(), ImmediateRetryScheduler())
    await post(db, message_id=1, content=REAL_REPORT, minutes=-2)
    await post(db, message_id=2, content="", attachments=2, minutes=-1)

    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert result.valid
    assert result.action == "none"


async def test_chat_is_not_treated_as_pending_report(db: Database) -> None:
    service = ReportService(db, RecordingReplySink(), ImmediateRetryScheduler())
    await post(db, message_id=1, content=CHAT, minutes=-5)
    await post(db, message_id=2, content="", attachments=1, minutes=-4)

    pending = await service.pending_for_author(CHANNEL, AUTHOR, 2)
    # Chat with no attachments is not a report awaiting evidence.
    assert 1 not in pending


# --- regression: the whole live sequence ----------------------------------


async def test_full_live_sequence(logs: None = None) -> None:
    """Report + 2 photos valid; bad enum warned after the window; chat silent."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(database_url=f"sqlite+aiosqlite:///{Path(tmp) / 'seq.db'}")
        db = Database(settings)
        await db.create_all()
        try:
            async with db.write_session() as session:
                await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
                await admin.bind_channel(
                    session,
                    guild_id=GUILD,
                    channel_id=CHANNEL,
                    module_key="reports",
                    schema_key="training_report",
                )

            sink = RecordingReplySink()
            service = ReportService(db, sink, ImmediateRetryScheduler())

            # 1. valid report with photos
            await post(db, message_id=1, content=REAL_REPORT, attachments=2)
            first = await service.process_message(CHANNEL, AUTHOR, 1)
            assert first is not None and first.valid

            # 2. chat
            await post(db, message_id=2, content=CHAT, minutes=1)
            chat = await service.process_message(CHANNEL, AUTHOR, 2)
            assert chat is not None and not chat.matched

            # 3. bad enum, old enough to warn
            await post(db, message_id=3, content=BAD_ENUM, minutes=-40)
            bad = await service.process_message(CHANNEL, AUTHOR, 3)
            assert bad is not None
            assert bad.matched and not bad.valid
            assert bad.action == "warned"

            assert len(sink.sent) == 1
        finally:
            await db.dispose()


async def test_ingestor_runs_the_service_pipeline(db: Database) -> None:
    """The bus path must reach the reports module, not just the ingestor."""
    sink = RecordingReplySink()
    service = ReportService(db, sink, ImmediateRetryScheduler())
    bus = EventBus()
    ingestor = Ingestor(db, bus)

    async def handler(event: MessageCreated) -> None:
        await service.process_message(
            event.message.channel_id, event.message.author_id, event.message.message_id
        )

    bus.subscribe("message_created", handler)

    snapshot = MessageSnapshot(
        message_id=1,
        channel_id=CHANNEL,
        guild_id=GUILD,
        author_id=AUTHOR,
        author_name="t",
        author_display_name="T",
        author_is_bot=False,
        content=REAL_REPORT,
        created_at=datetime.now(UTC),
    )
    await ingestor.handle(MessageCreated(message=snapshot))
    await bus.flush()
    await bus.aclose()

    async with db.session() as session:
        report = await repos.get_report_by_message(session, CHANNEL, 1)
    assert report is not None

"""End-to-end tests for the reports module, including the warning lifecycle.

These run against a real SQLite file and a recording reply sink, so they exercise the
same code the live bot runs: parse, persist, post, edit, clear.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from dsbot.config import Settings
from dsbot.core.bus import EventBus
from dsbot.core.events import AttachmentRef, Event, MessageCreated, MessageSnapshot
from dsbot.discord.reply import RecordingReplySink
from dsbot.ingest import Ingestor
from dsbot.modules.reports.authoring import STARTER_DEFINITION, export_forms, import_forms
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.modules.reports.service import ReportService
from dsbot.storage import repos
from dsbot.storage.db import Database

CHANNEL = 500
GUILD = 900
AUTHOR = 42


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'svc.db'}")
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
async def bound_db(db: Database) -> Database:
    from dsbot.storage import admin

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
    return db


async def store_message(
    db: Database,
    *,
    message_id: int,
    content: str,
    attachments: int = 0,
    minutes: int = 0,
    author: int = AUTHOR,
) -> None:
    snapshot = MessageSnapshot(
        message_id=message_id,
        channel_id=CHANNEL,
        guild_id=GUILD,
        author_id=author,
        author_name="instructor",
        author_display_name="Instr",
        author_is_bot=False,
        content=content,
        created_at=datetime.now(UTC) + timedelta(minutes=minutes),
        attachments=tuple(
            AttachmentRef(
                id=1000 + i,
                filename=f"photo{i}.png",
                url=f"https://cdn.example/{i}.png",
            )
            for i in range(attachments)
        ),
    )
    async with db.write_session() as session:
        await repos.upsert_message(session, snapshot)


VALID = """1. SOB | 0016
2. INF
3. Доказательства:
     - 3.1. фото
     - 3.2. фото"""


async def test_valid_report_persists_and_stores_fields(bound_db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content=VALID, attachments=2)

    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert result.matched
    assert result.valid
    assert result.field_count > 0

    async with bound_db.session() as session:
        report = await repos.get_report_by_message(session, CHANNEL, 1)
        assert report is not None
        assert report.status == "valid"
        assert report.schema_key == "training_report"
        assert report.attributed_user_id == AUTHOR
        assert report.attribution_method == "self"
    assert sink.sent == []


async def test_missing_evidence_is_warned_immediately(bound_db: Database) -> None:
    """Deferral was removed: a late warning feels broken, and it died on restart."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content="1. SOB | 0016\n2. INF\n3.")

    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert result.matched
    assert not result.valid
    assert result.action == "warned"
    assert len(sink.sent) == 1


async def test_warning_is_cleared_when_photos_arrive_later(bound_db: Database) -> None:
    """The immediate warning must still disappear once the report is completed."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content="1. SOB | 0016\n2. INF\n3.")
    first = await service.process_message(CHANNEL, AUTHOR, 1)
    assert first is not None and first.action == "warned"

    # Photo follows in a separate message, then the report is re-examined.
    await store_message(bound_db, message_id=2, content="", attachments=2)
    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert result.valid
    assert result.action == "cleared"


async def test_old_incomplete_report_is_warned(bound_db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content="1. SOB | 0016\n2. INF\n3.", minutes=-30)

    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert result.action == "warned"
    assert len(sink.sent) == 1
    channel_id, body = sink.sent[0]
    assert channel_id == CHANNEL
    assert f"<@{AUTHOR}>" in body


async def test_warning_quotes_the_report_message(bound_db: Database) -> None:
    """The warning should sit under the report it is about, not float loose."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content="1. SOB | 0016\n2. INF\n3.")

    await service.process_message(CHANNEL, AUTHOR, 1)

    assert len(sink.sent) == 1
    assert sink.references == [1]


async def test_warning_mentions_the_missing_field(bound_db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content="1. SOB | 0016\n2. INF\n3.", minutes=-30)
    await service.process_message(CHANNEL, AUTHOR, 1)

    body = sink.sent[0][1]
    assert "фото" in body.lower()


async def test_warning_is_edited_not_duplicated(bound_db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content="1. SOB | 0016\n2. INF\n3.", minutes=-30)
    await service.process_message(CHANNEL, AUTHOR, 1)
    await service.process_message(CHANNEL, AUTHOR, 1)

    assert len(sink.sent) == 1
    assert len(sink.edited) == 1


async def test_fixing_the_report_clears_the_warning(bound_db: Database) -> None:
    """The promised behaviour: edit the message, the warning disappears by itself."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content="1. SOB | 0016\n2. INF\n3.", minutes=-30)
    await service.process_message(CHANNEL, AUTHOR, 1)
    assert len(sink.sent) == 1

    # Author edits the message to a complete report.
    await store_message(bound_db, message_id=1, content=VALID, attachments=2, minutes=-30)
    result = await service.process_message(CHANNEL, AUTHOR, 1)

    assert result is not None
    assert result.valid
    assert result.action == "cleared"
    assert len(sink.cleared) == 1

    async with bound_db.session() as session:
        assert await repos.get_validation_message(session, result.report_id) is None


async def test_followup_photo_completes_the_report(bound_db: Database) -> None:
    """The documented workflow: report first, photo in the next message."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content="1. SOB | 0016\n2. INF\n3.")
    await store_message(bound_db, message_id=2, content="", attachments=2, minutes=1)

    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert result.matched
    assert result.valid, result.issues


async def test_chat_in_report_channel_is_ignored_not_warned(bound_db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(
        bound_db, message_id=1, content="народ, сегодня кто идёт на тренировку?", minutes=-30
    )

    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None
    assert not result.matched
    assert result.action == "stored"
    assert sink.sent == []


async def test_bot_messages_are_not_reports(bound_db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    snapshot = MessageSnapshot(
        message_id=1,
        channel_id=CHANNEL,
        guild_id=GUILD,
        author_id=777,
        author_name="bot",
        author_display_name="bot",
        author_is_bot=True,
        content=VALID,
        created_at=datetime.now(UTC),
    )
    async with bound_db.write_session() as session:
        await repos.upsert_message(session, snapshot)

    assert await service.process_message(CHANNEL, 777, 1) is None


async def test_unbound_channel_is_ignored(db: Database) -> None:
    service = ReportService(db, RecordingReplySink())
    assert await service.channel_context(CHANNEL) is None


async def test_reparse_replaces_fields_not_appends(bound_db: Database) -> None:
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store_message(bound_db, message_id=1, content="1. SOB | 0016\n2. INF", minutes=-30)
    await service.process_message(CHANNEL, AUTHOR, 1)

    await store_message(bound_db, message_id=1, content=VALID, attachments=2, minutes=-30)
    result = await service.process_message(CHANNEL, AUTHOR, 1)
    assert result is not None

    async with bound_db.session() as session:
        from sqlalchemy import func, select

        from dsbot.storage.models import ReportField

        count = await session.scalar(
            select(func.count(ReportField.id)).where(ReportField.report_id == result.report_id)
        )
    assert count == len(DEFAULT_SCHEMAS[0].fields)


async def test_live_and_replay_agree(db: Database) -> None:
    """The same event through the ingestor produces the same stored message."""
    from dsbot.storage import admin

    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
        await admin.bind_channel(
            session,
            guild_id=GUILD,
            channel_id=CHANNEL,
            module_key="reports",
            schema_key="training_report",
        )

    bus = EventBus()
    ingestor = Ingestor(db, bus)
    snapshot = MessageSnapshot(
        message_id=99,
        channel_id=CHANNEL,
        guild_id=GUILD,
        author_id=AUTHOR,
        author_name="a",
        author_display_name="a",
        author_is_bot=False,
        content=VALID,
        created_at=datetime.now(UTC),
    )
    from dsbot.storage.models import RawEvent

    event: Event = MessageCreated(message=snapshot)
    await ingestor.handle(event)
    await bus.aclose()

    async with db.session() as session:
        stored = await repos.get_message(session, CHANNEL, 99)
        raw = await session.scalar(select(RawEvent))
    assert stored is not None
    assert stored.content == VALID
    assert raw is not None
    assert raw.payload["message"]["content"] == VALID


# --- authoring -------------------------------------------------------------


async def test_export_returns_bound_forms(bound_db: Database) -> None:
    async with bound_db.session() as session:
        bundle = await export_forms(session, CHANNEL)
    assert bundle.binding_id is not None
    assert len(bundle.schemas) == 1
    assert bundle.schemas[0]["key"] == "training_report"


async def test_roundtrip_export_import(bound_db: Database) -> None:
    import json

    async with bound_db.session() as session:
        bundle = await export_forms(session, CHANNEL)

    payload = json.loads(bundle.to_json())
    async with bound_db.write_session() as session:
        result = await import_forms(session, channel_id=CHANNEL, payload=payload)
    assert result.imported == ["training_report"]


async def test_import_creates_a_new_form_for_a_new_channel(db: Database) -> None:
    definition = dict(STARTER_DEFINITION)
    definition["key"] = "custom_form"

    async with db.write_session() as session:
        result = await import_forms(
            session,
            channel_id=777,
            payload={"schemas": [definition]},
            guild_id=GUILD,
        )
    assert result.imported == ["custom_form"]
    assert "custom_form" in result.created


async def test_invalid_definition_is_rejected_before_writing(db: Database) -> None:
    bad = {"key": "bad", "fields": [{"id": "x", "path": "not-a-number"}]}
    with pytest.raises(ValueError):
        async with db.write_session() as session:
            await import_forms(session, channel_id=777, payload={"schemas": [bad]}, guild_id=GUILD)

    async with db.session() as session:
        from sqlalchemy import func, select

        from dsbot.storage.models import FormSchema

        count = await session.scalar(
            select(func.count(FormSchema.id)).where(FormSchema.key == "bad")
        )
    assert count == 0


async def test_unbound_channel_import_requires_guild(db: Database) -> None:
    with pytest.raises(ValueError, match="guild"):
        async with db.write_session() as session:
            await import_forms(
                session,
                channel_id=888,
                payload={"schemas": [STARTER_DEFINITION]},
            )


async def test_custom_form_actually_drives_the_parser(db: Database) -> None:
    """A per-channel form must change behaviour, not just be stored."""
    custom = {
        "key": "ops_report",
        "title": "Operation report",
        "locale": "ru",
        "fields": [
            {
                "id": "commander",
                "labels": ["командир"],
                "path": "1",
                "type": "name_ref",
                "required": True,
            },
            {
                "id": "outcome",
                "labels": ["результат"],
                "path": "2",
                "type": "text",
                "required": True,
            },
        ],
        "attribution_field": "commander",
    }
    from dsbot.storage import admin

    async with db.write_session() as session:
        await import_forms(session, channel_id=555, payload={"schemas": [custom]}, guild_id=GUILD)
        await admin.ensure_schema_seed(session, ())

    async with db.write_session() as session:
        snapshot = MessageSnapshot(
            message_id=1,
            channel_id=555,
            guild_id=GUILD,
            author_id=AUTHOR,
            author_name="deputy",
            author_display_name="deputy",
            author_is_bot=False,
            content="1. Командир: Майор Иванов\n2. Результат: успех",
            created_at=datetime.now(UTC),
        )
        await repos.upsert_message(session, snapshot)

    sink = RecordingReplySink()
    service = ReportService(db, sink)
    result = await service.process_message(555, AUTHOR, 1)

    assert result is not None
    assert result.matched
    assert result.valid, result.issues

    async with db.session() as session:
        report = await repos.get_report_by_message(session, 555, 1)
        assert report is not None
        # Attribution points at the commander field, not the author.
        assert report.attributed_field == "commander"
        assert report.attributed_raw == "Майор Иванов"

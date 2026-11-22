"""Builds a database shaped like the incident, then runs the audit tool on it.

Creates two reports:
  * one from six months ago, warned only after an EDIT event - the incident shape
  * one from now, warned after a CREATE event - normal

and confirms the audit separates them. This is the proof that pulling `dsbot.db` off the
server is enough to recover the message ids, with no log file and no Discord access.

    uv run python scripts/e2e-audit-warnings.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta

from dsbot.config import Settings
from dsbot.core.bus import EventBus
from dsbot.core.events import MessageCreated, MessageEdited, MessageSnapshot
from dsbot.discord.reply import RecordingReplySink
from dsbot.ingest import Ingestor
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.modules.reports.service import ReportService
from dsbot.storage import admin
from dsbot.storage.db import Database

CHANNEL = 1274460260941434901
GUILD = 1274460259661910149
AUTHOR = 263344369451401227
OLD_MESSAGE = 1000000000000000001
NEW_MESSAGE = 1000000000000000002
INCOMPLETE = "1. SOB | 0016\n2. INF\n3."


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db_path = tmp / "dsbot.db"
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}"))
    try:
        await db.create_all()
        await db.prepare()

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

        bus = EventBus()
        ingestor = Ingestor(db, bus)
        # No age limit: this script is about the audit trail, not the guard.
        service = ReportService(db, RecordingReplySink(), warn_max_age=None)
        bus.subscribe(
            "message_edited",
            lambda event: service.process_message(
                event.message.channel_id, event.message.author_id, event.message.message_id
            ),
        )
        bus.subscribe(
            "message_created",
            lambda event: service.process_message(
                event.message.channel_id, event.message.author_id, event.message.message_id
            ),
        )

        def snapshot(message_id: int, created: datetime) -> MessageSnapshot:
            return MessageSnapshot(
                message_id=message_id,
                channel_id=CHANNEL,
                guild_id=GUILD,
                author_id=AUTHOR,
                author_name="foggytown",
                author_display_name="foggytown",
                author_is_bot=False,
                content=INCOMPLETE,
                created_at=created,
            )

        # The incident: a six-month-old report that only ever arrived as an EDIT event.
        old_created = datetime.now(UTC) - timedelta(days=180)
        from dsbot.storage import repos

        async with db.write_session() as session:
            await repos.upsert_message(session, snapshot(OLD_MESSAGE, old_created))
        edited = snapshot(OLD_MESSAGE, old_created)
        await ingestor.handle(
            MessageEdited(message=edited, previous_content="old text"),
        )

        # Normal: a report posted now, warned as a new message.
        now = datetime.now(UTC)
        created_event = MessageCreated(message=snapshot(NEW_MESSAGE, now))
        await ingestor.handle(created_event)

        # Let the lanes drain before the database is read by the audit tool.
        await bus.flush()
        await bus.aclose()
    finally:
        await db.dispose()

    conn = sqlite3.connect(db_path)
    warn_rows = conn.execute("SELECT COUNT(*) FROM validation_messages").fetchone()[0]
    conn.close()
    print(f"database built: {warn_rows} warning(s) on record\n")

    # Run the real audit tool, capturing its output, rather than reimplementing the query
    # here: a test that reimplements the thing under test proves nothing. The file name
    # has a hyphen, so it is loaded by path rather than imported by name.
    import contextlib
    import importlib.util
    import io

    spec = importlib.util.spec_from_file_location(
        "audit_warnings", pathlib.Path("scripts/audit-warnings.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = module.audit(db_path, old_days=1.0, limit=50)
    text = buffer.getvalue()
    print(text)
    if code != 0:
        raise SystemExit(f"audit tool failed with {code}")
    assert str(OLD_MESSAGE) in text, "the old report message id must appear in the output"
    assert str(NEW_MESSAGE) in text, "the new report message id must appear in the output"
    assert "ONLY seen as an EDIT" in text, "the incident trigger must be identified"
    assert "OLD WARNINGS" in text, "old warnings must be summarised"
    print("OK: the database alone reveals the warned message ids and how each was triggered")


if __name__ == "__main__":
    asyncio.run(main())

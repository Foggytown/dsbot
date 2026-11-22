"""Proves the age guard: a six-month-old report is stored, never warned about.

Reproduces the production incident - old messages being warned about - and shows the
fix, including what the log says now.

    uv run python scripts/e2e-warning-age.py
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from datetime import UTC, datetime, timedelta

import structlog

from dsbot.config import Settings
from dsbot.core.events import MessageSnapshot
from dsbot.discord.reply import RecordingReplySink
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.modules.reports.service import ReportService
from dsbot.storage import admin, repos
from dsbot.storage.db import Database

CHANNEL = 1274460260941434901
GUILD = 1274460259661910149
AUTHOR = 263344369451401227
INCOMPLETE = "1. SOB | 0016\n2. INF\n3."


async def store(db: Database, *, message_id: int, age: timedelta) -> None:
    async with db.write_session() as session:
        await repos.upsert_message(
            session,
            MessageSnapshot(
                message_id=message_id,
                channel_id=CHANNEL,
                guild_id=GUILD,
                author_id=AUTHOR,
                author_name="foggytown",
                author_display_name="foggytown",
                author_is_bot=False,
                content=INCOMPLETE,
                created_at=datetime.now(UTC) - age,
            ),
        )


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'w.db').as_posix()}"))
    sink = RecordingReplySink()
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

        # The default service, exactly as the live bot builds it apart from settings.
        service = ReportService(db, sink)

        cases = [
            ("6 months old", timedelta(days=180)),
            ("3 days old", timedelta(days=3)),
            ("10 minutes old", timedelta(minutes=10)),
        ]
        print(f"{'report age':<18} {'action':<10} {'warning posted?'}")
        print("-" * 52)
        for index, (label, age) in enumerate(cases, start=1000):
            await store(db, message_id=index, age=age)
            result = await service.process_message(CHANNEL, AUTHOR, index)
            assert result is not None
            posted = "YES" if result.action == "warned" else "no"
            print(f"{label:<18} {result.action:<10} {posted}")

        warned = [mid for mid, _ in sink.sent]
        print()
        print(f"warnings actually posted: {len(sink.sent)}")
        assert len(sink.sent) == 1, "only a report from within the last day may be warned about"

        # The stored warning names the report, so the log is traceable.
        async with db.session() as session:
            newest = await repos.get_report_by_message(session, CHANNEL, 1002)
            assert newest is not None
            record = await repos.get_validation_message(session, newest.id)
        assert record is not None
        print(
            f"warning row: message_id={record.message_id} "
            f"report_message_id={record.report_message_id}"
        )
        assert record.report_message_id == 1002
        assert record.message_id != record.report_message_id

        # And the old report is still on record, just silent.
        async with db.session() as session:
            old = await repos.get_report_by_message(session, CHANNEL, 1000)
        assert old is not None and old.status == "invalid"
        print()
        print("the 6-month-old report is stored as 'invalid' and was never warned about")
        print("warned ids:", warned)
        print()
        print("OK: old reports no longer nag the channel, and warnings name both ids")
    finally:
        await db.dispose()


if __name__ == "__main__":
    structlog.configure()
    asyncio.run(main())

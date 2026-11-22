"""An old report with an existing warning: does fixing part of it update the warning?

The age guard was meant to stop the bot creating a *new* warning about a months-old
message. It also blocked updating an existing one, so a warning about an old report froze:
correcting one of several problems changed nothing, and the stale complaint stayed in the
channel forever.

    uv run python scripts/repro-stale-warning.py
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from datetime import UTC, datetime

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
LINK = "https://discord.com/channels/1274402776696488108/1339645935348613191/1348671185625415771"

# A report from 2025, exactly like the ones in the channel that have the link in field 3.
MSG = 1405162864922071180
CREATED = datetime(2025, 8, 13, 12, 15, 33, tzinfo=UTC)

BROKEN = f"""1. SOB | 0016
2. INF
3. {LINK}
4. """

# The author moves the link to field 4. Photos are still missing, so it is still invalid -
# but the link complaint should be gone.
FIXED_LINK = f"""1. SOB | 0016
2. INF
3. Доказательства:
4. {LINK}"""


async def store(db: Database, content: str, *, edited: bool) -> None:
    async with db.write_session() as session:
        await repos.upsert_message(
            session,
            MessageSnapshot(
                message_id=MSG,
                channel_id=CHANNEL,
                guild_id=GUILD,
                author_id=AUTHOR,
                author_name="fireser",
                author_display_name="fireser",
                author_is_bot=False,
                content=content,
                created_at=CREATED,
                edited_at=datetime.now(UTC) if edited else None,
            ),
        )


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp / 's.db'}"))
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
        # The live bot's service: the age guard is on.
        service = ReportService(db, sink)

        print("1. an OLD report with the link in field 3, no warning yet")
        await store(db, BROKEN, edited=False)
        first = await service.process_message(CHANNEL, AUTHOR, MSG)
        print(f"   action={first.action} issues={first.issues}")
        print(f"   warnings posted: {len(sink.sent)}  (0 is correct: too old to nag)")
        print()

        # Simulate the warning that already exists in the channel from before the guard.
        if not sink.sent:
            print("   planting the warning that is already in the channel")
            await store(db, BROKEN, edited=False)
            without_guard = ReportService(db, sink, warn_max_age=None)
            await without_guard.process_message(CHANNEL, AUTHOR, MSG)
            print(f"   warnings posted: {len(sink.sent)}")
            for line in sink.sent[-1][1].splitlines():
                print(f"     {line}")
        print()

        print("2. the author moves the link into field 4 (photos still missing)")
        await store(db, FIXED_LINK, edited=True)
        second = await service.process_message(CHANNEL, AUTHOR, MSG)
        print(f"   action={second.action} valid={second.valid}")
        print(f"   issues now={second.issues}")
        print(f"   edits so far: {len(sink.edited)}  cleared: {len(sink.cleared)}")
        print()
        body = sink.edited[-1][2] if sink.edited else sink.sent[-1][1]
        print("   WARNING STILL IN THE CHANNEL:")
        for line in body.splitlines():
            print(f"     {line}")
        print()
        if "пункте 3" in body:
            print("PROBLEM: the link complaint is still there after the link was moved")
            print("         the warning is frozen because the report is old")
        else:
            print("OK: the warning was updated and no longer mentions the link")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

"""Live case, with the form the channel actually uses.

uv run python scripts/repro-live-edit.py
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from datetime import UTC, datetime

from dsbot.bot import _edit_changed_message
from dsbot.config import Settings
from dsbot.core.bus import EventBus
from dsbot.core.events import AttachmentRef, MessageCreated, MessageEdited, MessageSnapshot
from dsbot.discord.reply import RecordingReplySink
from dsbot.ingest import Ingestor
from dsbot.modules.reports.service import ReportService
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.storage import admin
from dsbot.storage.db import Database

CHANNEL = 1274460260941434901
GUILD = 1274460259661910149
AUTHOR = 263344369451401227
LINK = "https://discord.com/channels/1274402776696488108/1339645935348613191/1348671185625415771"
MSG = 1573991757002055723

PHOTOS = "3. Доказательства:\n     - 3.1. фото\n     - 3.2. фото"


def snap(
    message_id: int, content: str, *, attachments: int, edited: bool = False
) -> MessageSnapshot:
    return MessageSnapshot(
        message_id=message_id,
        channel_id=CHANNEL,
        guild_id=GUILD,
        author_id=AUTHOR,
        author_name="foggytown",
        author_display_name="foggytown",
        author_is_bot=False,
        content=content,
        created_at=datetime.now(UTC),
        edited_at=datetime.now(UTC) if edited else None,
        attachments=tuple(
            AttachmentRef(id=8000 + i, filename=f"p{i}.png", url="https://cdn.example/x.png")
            for i in range(attachments)
        ),
    )


BEFORE = f"1. SOB | 0016\n2. INF\n{PHOTOS}\n4."
AFTER = f"1. SOB | 0016\n2. INF\n{PHOTOS}\n4. {LINK}"

# The same, but the link was pasted into item 3.
LINK_IN_3 = f"1. SOB | 0016\n2. INF\n3. {LINK}\n4."
LINK_MOVED = f"1. SOB | 0016\n2. INF\n3. фото\n4. {LINK}"

# Exactly what is in the channel: a T report whose NRP idn is 3 digits, so the report can
# never become valid by moving the link alone.
REAL_IN_3 = f"1. SOB | 00/0007 | 813\n2. T\n3. {LINK}\n4."
REAL_MOVED = f"1. SOB | 00/0007 | 813\n2. T\n3. фото\n4. {LINK}"


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp / 'l.db'}"))
    sink = RecordingReplySink()
    bus = EventBus()
    ingestor = Ingestor(db, bus)
    service = ReportService(db, sink)

    async def handle(event: MessageCreated | MessageEdited) -> None:
        r = await service.process_message(
            event.message.channel_id, event.message.author_id, event.message.message_id
        )
        kind = "created" if isinstance(event, MessageCreated) else "edited "
        print(f"   [{kind}] action={r.action if r else None} issues={r.issues if r else None}")

    bus.subscribe("message_created", handle)
    bus.subscribe("message_edited", handle)

    try:
        await db.create_all()
        await db.prepare()
        schema = parse_form_file(pathlib.Path("forms/training.yaml"))
        async with db.write_session() as session:
            await admin.ensure_schema_seed(session, [schema])
            await admin.bind_channel(
                session,
                guild_id=GUILD,
                channel_id=CHANNEL,
                module_key="reports",
                schema_key=schema.key,
                locale="ru",
            )

        for index, (label, before, after, attachments) in enumerate(
            (
                ("CASE A: link missing, then added to item 4", BEFORE, AFTER, 2),
                (
                    "CASE C: YOUR CASE - link in item 3, then moved to item 4",
                    LINK_IN_3,
                    LINK_MOVED,
                    2,
                ),
            ),
            start=1,
        ):
            message_id = MSG + index * 10
            print(label)
            print("-" * 70)
            await ingestor.handle(
                MessageCreated(message=snap(message_id, before, attachments=attachments))
            )
            await bus.flush()
            if sink.sent:
                print("   WARNING:")
                for line in sink.sent[-1][1].splitlines():
                    print(f"     {line}")
            else:
                print("   (no warning posted)")
            print()

            print()
            print("   --- the author edits it: does the gateway guard let the edit through? ---")
            wire = {
                "id": str(message_id),
                "channel_id": str(CHANNEL),
                "content": after,
                "timestamp": "2026-11-22T12:00:00.000000+00:00",
                "edited_timestamp": "2026-11-22T13:00:00.000000+00:00",
            }

            class _Cached:
                def __init__(self, content: str) -> None:
                    self.content = content

            class _Payload:
                def __init__(self, data: dict, cached: object) -> None:
                    self.data = data
                    self.cached_message = cached

            accepted = _edit_changed_message(_Payload(wire, _Cached(before)))
            print(f"   _edit_changed_message -> {accepted}")
            if not accepted:
                print("   PROBLEM: the gateway drops a genuine edit, so nothing re-parses")
                sink.sent.clear()
                sink.edited.clear()
                sink.cleared.clear()
                continue

            await ingestor.handle(
                MessageEdited(
                    message=snap(message_id, after, attachments=attachments, edited=True),
                    previous_content=before,
                )
            )
            await bus.flush()
            print(
                f"   after the edit: sent={len(sink.sent)} edited={len(sink.edited)}"
                f" cleared={len(sink.cleared)}"
            )
            if sink.cleared:
                print("   OK: warning deleted")
            elif sink.edited:
                print("   warning EDITED, still visible:")
                for line in sink.edited[-1][2].splitlines():
                    print(f"     {line}")
            else:
                print("   PROBLEM: warning untouched - frozen")
            print()
            # Reset between cases.
            sink.sent.clear()
            sink.edited.clear()
            sink.cleared.clear()
    finally:
        await bus.aclose()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

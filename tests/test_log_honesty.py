"""Two log lines that described the wrong event.

Both came from the production log being read as evidence:

  warning_posted  message_id=1574441799639105619 report_id=45   (creation)
  warning_posted  message_id=1574441799639105619 report_id=45   (edit)
  warning_posted  message_id=1574441799639105619 report_id=45   (edit)

Three identical lines with one message id look like duplicate posting. Nothing was posted
after the first: the warning was being *edited*. The line was named for the wrong event.

  verdict_changed  by_user_id=None previous=None report_id=45 verdict=pending

A report being evaluated for the first time, with no verdict yet, is not a change, and
`by_user_id=None` on a line about a decision reads like an anonymous reviewer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from dsbot.config import Settings
from dsbot.core.events import AttachmentRef, MessageSnapshot
from dsbot.discord.reply import RecordingReplySink
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.modules.reports.service import ReportService
from dsbot.storage import admin, repos
from dsbot.storage.db import Database

CHANNEL = 950
GUILD = 951
AUTHOR = 952

INCOMPLETE = "1. SOB | 0016\n2. PEPE\n3. фото"
COMPLETE = "1. SOB | 0016\n2. INF\n3. фото\n4. https://discord.com/channels/1/2/3"

# Two photos, because the real form requires at least two for any non-T qualification.
# The defect the fixture relies on is the unknown qualification, which is independent of
# the photos, so the "fixed" report is valid and the warning is cleared rather than edited.
PHOTOS = 2


@pytest.fixture
async def bound(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'logs.db'}"))
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


async def store(db: Database, content: str) -> None:
    async with db.write_session() as session:
        await repos.upsert_message(
            session,
            MessageSnapshot(
                message_id=1,
                channel_id=CHANNEL,
                guild_id=GUILD,
                author_id=AUTHOR,
                author_name="tester",
                author_display_name="tester",
                author_is_bot=False,
                content=content,
                created_at=datetime.now(UTC),
                attachments=tuple(
                    AttachmentRef(
                        id=index + 1,
                        filename=f"p{index}.png",
                        url=f"https://cdn.example/p{index}.png",
                    )
                    for index in range(PHOTOS)
                ),
            ),
        )


def warning_events(events: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(event["event"])
        for event in events
        if str(event.get("event", "")).startswith("warning_")
    ]


async def test_editing_a_warning_logs_an_edit_not_a_post(bound: Database) -> None:
    """A single warning must not look like three postings."""
    service = ReportService(bound, RecordingReplySink(), warn_max_age=None)
    await store(bound, INCOMPLETE)

    with capture_logs() as created:
        await service.process_message(CHANNEL, AUTHOR, 1)
    assert warning_events(created) == ["warning_posted"]

    await store(bound, COMPLETE)
    with capture_logs() as edited:
        await service.process_message(CHANNEL, AUTHOR, 1)

    events = warning_events(edited)
    assert "warning_posted" not in events, f"nothing was posted, yet the log said {events}"
    assert events == ["warning_cleared"], "the fix clears the warning"


async def test_an_unchanged_warning_is_edited_not_reposted(bound: Database) -> None:
    """Re-parsing the same broken report twice updates one message."""
    service = ReportService(bound, RecordingReplySink(), warn_max_age=None)
    await store(bound, INCOMPLETE)

    with capture_logs() as first:
        await service.process_message(CHANNEL, AUTHOR, 1)
    assert warning_events(first) == ["warning_posted"]

    with capture_logs() as second:
        await service.process_message(CHANNEL, AUTHOR, 1)

    assert warning_events(second) == ["warning_edited"]


async def test_a_first_pending_verdict_is_not_a_change(bound: Database) -> None:
    """`previous=None -> pending` is the initial state, not a transition."""
    from dsbot.modules.verdict.service import VerdictService

    await store(bound, INCOMPLETE)
    service = ReportService(bound, RecordingReplySink(), warn_max_age=None)
    await service.process_message(CHANNEL, AUTHOR, 1)

    verdicts = VerdictService(bound)
    with capture_logs() as events:
        await verdicts.on_reaction(1)

    names = [str(event.get("event")) for event in events]
    assert "verdict_changed" not in names, f"nothing changed, yet the log said {names}"

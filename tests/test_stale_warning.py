"""An existing warning must follow the report, however old the report is.

The age guard was added to stop the bot *creating* complaints about months-old messages.
It also blocked *maintaining* an existing one, which froze warnings: fixing one of several
problems changed nothing, and a fully corrected report kept its warning forever.

That is the "edit it and it clears itself" promise broken, and it is what made a reporter
say "I moved the link into field 4 and the message did not disappear".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.core.events import AttachmentRef, MessageSnapshot
from dsbot.discord.reply import RecordingReplySink
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.modules.reports.service import ReportService
from dsbot.storage import admin, repos
from dsbot.storage.db import Database

CHANNEL = 610
GUILD = 902
AUTHOR = 44
LINK = "https://discord.com/channels/1274402776696488108/1339645935348613191/1348671185625415771"
MSG = 1405162864922071180
OLD = datetime.now(UTC) - timedelta(days=400)


@pytest.fixture
async def bound_db(tmp_path: Path) -> AsyncIterator[Database]:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
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


async def store(db: Database, content: str, *, attachments: int, edited: bool = False) -> None:
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
                created_at=OLD,
                edited_at=datetime.now(UTC) if edited else None,
                attachments=tuple(
                    AttachmentRef(
                        id=7000 + i, filename=f"p{i}.png", url="https://cdn.example/x.png"
                    )
                    for i in range(attachments)
                ),
            ),
        )


BROKEN = f"""1. SOB | 0016
2. INF
3. {LINK}
4. """

LINK_FIXED = f"""1. SOB | 0016
2. INF
3. Доказательства:
     - 3.1. фото
     - 3.2. фото
4. {LINK}"""

# The same report with the link moved but no photo lines at all, for the case where the
# author has not got the evidence yet: still invalid, but no longer a link problem.
LINK_FIXED_PLAIN = f"""1. SOB | 0016
2. INF
3. Доказательства:
4. {LINK}"""


async def test_an_old_report_does_not_get_a_new_warning(bound_db: Database) -> None:
    """The guard's original purpose, still enforced."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store(bound_db, BROKEN, attachments=2)

    result = await service.process_message(CHANNEL, AUTHOR, MSG)

    assert result is not None
    assert result.action == "stored"
    assert sink.sent == [], "no new warning may be created for a 400-day-old report"


async def test_an_existing_warning_on_an_old_report_is_still_maintained(
    bound_db: Database,
) -> None:
    """The bug: a warning about an old report was frozen, so fixes did nothing."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store(bound_db, BROKEN, attachments=2)

    # Plant the warning that was created before the age guard existed.
    legacy = ReportService(bound_db, sink, warn_max_age=None)
    first = await legacy.process_message(CHANNEL, AUTHOR, MSG)
    assert first is not None and first.action == "warned"
    assert len(sink.sent) == 1
    assert "пункте 3" in sink.sent[0][1]

    # The author fixes only the link; photos are still missing. The photo follow-up was a
    # separate message, so re-storing the head with zero attachments is a fresh state.
    await store(bound_db, LINK_FIXED_PLAIN, attachments=0, edited=True)
    second = await service.process_message(CHANNEL, AUTHOR, MSG)

    assert second is not None
    assert not second.valid
    assert "link_in_wrong_field" not in second.issues
    assert sink.edited, "the existing warning must be edited, not left frozen"
    latest = sink.edited[-1][2]
    assert "пункте 3" not in latest, "the fixed complaint must disappear from the warning"
    assert "фото" in latest, "the remaining problem must still be shown"


async def test_an_old_report_that_becomes_valid_has_its_warning_cleared(
    bound_db: Database,
) -> None:
    """The full fix must remove the message, not merely stop complaining about it."""
    sink = RecordingReplySink()
    legacy = ReportService(bound_db, sink, warn_max_age=None)
    await store(bound_db, BROKEN, attachments=2)
    first = await legacy.process_message(CHANNEL, AUTHOR, MSG)
    assert first is not None and first.action == "warned"

    # Everything is now correct: link in field 4, two photos.
    await store(bound_db, LINK_FIXED, attachments=2, edited=True)
    service = ReportService(bound_db, sink)
    second = await service.process_message(CHANNEL, AUTHOR, MSG)

    assert second is not None
    assert second.valid, "the report is correct now"
    assert sink.cleared, "the warning must be deleted from the channel"


async def test_no_warning_and_old_stays_quiet_even_if_still_invalid(bound_db: Database) -> None:
    """An old, still-broken report with no warning must not gain one later."""
    sink = RecordingReplySink()
    service = ReportService(bound_db, sink)
    await store(bound_db, BROKEN, attachments=0)

    for _ in range(3):
        result = await service.process_message(CHANNEL, AUTHOR, MSG)
        assert result is not None
        assert result.action == "stored"

    assert sink.sent == []

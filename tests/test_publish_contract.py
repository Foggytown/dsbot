"""Two promises about publishing that were not kept.

1. `/dsbot report-target` said "not saved" after it had already saved. The access check
   ran after the write, so a forbidden destination was stored and every later close
   failed.
2. A report split across pages could half-publish and still be recorded as done: `publish`
   returned the pages it managed to send, and the caller treated a non-empty list as
   success, advancing the schedule while pages two and three never appeared.

The probe also existed twice - in `commands.py` and `close.py` - with different wording.
It lives in `discord/reply.py` now, so the two callers cannot drift.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dsbot.config import Settings
from dsbot.modules.quotas.counting import CountSummary, MemberTally
from dsbot.modules.quotas.period import Period
from dsbot.modules.quotas.render import RenderContext
from dsbot.modules.quotas.service import CountingService, CountOutcome, CountRequest
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.storage import admin
from dsbot.storage.db import Database


class _RefusingSink:
    """Fails every send, the way a channel without permission does."""

    def __init__(self) -> None:
        self.last_error = ""
        self.sent = 0

    async def send(self, *args: Any, **kwargs: Any) -> None:
        self.last_error = "нет доступа к каналу"
        return None

    async def edit(self, *args: Any, **kwargs: Any) -> bool:
        return False

    async def clear(self, *args: Any, **kwargs: Any) -> bool:
        return False


class _PartialSink:
    """Sends the first page, then refuses - the half-published case."""

    def __init__(self) -> None:
        self.last_error = ""
        self.sent = 0
        self._next = 5000

    async def send(self, channel_id: int, content: str, **kwargs: Any) -> int | None:
        if self.sent >= 1:
            self.last_error = "вторая страница не отправилась"
            return None
        self.sent += 1
        self._next += 1
        return self._next

    async def edit(self, *args: Any, **kwargs: Any) -> bool:
        return False

    async def clear(self, *args: Any, **kwargs: Any) -> bool:
        return True


@pytest.fixture
async def bound(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'pub.db'}"))
    await db.create_all()
    await db.prepare()
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
        await admin.bind_channel(
            session,
            guild_id=930,
            channel_id=931,
            module_key="reports",
            schema_key="training_report",
        )
    yield db
    await db.dispose()


def many_people(count: int) -> CountSummary:
    return CountSummary(
        tallies=[
            MemberTally(
                user_id=10_000 + index,
                display_name=f"Person {index} with a reasonably long display name",
                tier_label="Instructor",
                tier_ordinal=10,
                approved_count=1,
                required_count=1,
            )
            for index in range(count)
        ]
    )


async def _outcome(db: Database, summary: CountSummary) -> tuple[CountRequest, CountOutcome]:
    async with db.session() as session:
        from dsbot.storage import repos

        binding = await repos.get_binding_by_channel(session, 931)
    assert binding is not None
    period = Period(
        start=datetime(2026, 11, 16, tzinfo=UTC), end=datetime(2026, 11, 23, tzinfo=UTC)
    )
    request = CountRequest(binding_id=binding.id, channel_id=931, period=period)
    context = RenderContext(period_label="16.11-22.11", timezone="Europe/Moscow")
    return request, CountOutcome(summary=summary, context=context)


async def test_a_publish_that_sends_nothing_reports_the_reason(bound: Database) -> None:
    request, outcome = await _outcome(bound, many_people(2))
    sink = _RefusingSink()
    service = CountingService(bound, sink)  # type: ignore[arg-type]

    posted = await service.publish(request, outcome, target_channel_id=940)

    assert posted == []
    assert service.last_error, "the service must record why it could not publish"


async def test_a_partial_publish_is_not_reported_as_success(bound: Database) -> None:
    """The page count tells the caller that not everything arrived."""
    request, outcome = await _outcome(bound, many_people(120))
    pages = len(outcome.messages())
    assert pages > 1, "this test needs a multi-page report"

    sink = _PartialSink()
    service = CountingService(bound, sink)  # type: ignore[arg-type]
    posted = await service.publish(request, outcome, target_channel_id=940)

    assert len(posted) < pages
    assert service.last_error, "a short publication must say so"


def test_the_trailer_never_overflows_the_last_page() -> None:
    """The 'not in the role list' block and the footer are length-checked now.

    They used to be concatenated onto the last page unconditionally, so a full page plus
    the trailer exceeded Discord's limit and the send failed after earlier pages were out.
    """
    from dsbot.modules.quotas.render import MAX_MESSAGE_LENGTH

    summary = many_people(200)
    context = RenderContext(period_label="16.11-22.11", timezone="Europe/Moscow")
    pages = __import__("dsbot.modules.quotas.render", fromlist=["render_message"]).render_message(
        summary, context
    )

    for page in pages:
        assert len(page) <= MAX_MESSAGE_LENGTH, (
            f"page of {len(page)} chars exceeds the {MAX_MESSAGE_LENGTH} limit"
        )


async def test_the_probe_has_one_implementation() -> None:
    """`can_post_to` lives in reply.py; commands.py no longer defines its own copy."""
    from dsbot.discord import commands as commands_module
    from dsbot.discord import reply as reply_module
    from dsbot.modules.quotas import close as close_module

    assert hasattr(reply_module, "can_post_to")
    assert not hasattr(commands_module, "can_post_to"), "the duplicate must be gone"
    assert hasattr(close_module, "destination_problem")

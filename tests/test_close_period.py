"""Closing a period end to end, against a real database and a recording sink.

The behaviours here are the ones that would be expensive to get wrong in production:
advancing a schedule after a failed publish, publishing a stale week, and posting a
duplicate instead of editing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dsbot.config import Settings
from dsbot.discord.reply import RecordingReplySink
from dsbot.modules.quotas import close as close_module
from dsbot.modules.quotas.schedule import parse_close
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.storage import admin, repos
from dsbot.storage.db import Database
from dsbot.storage.models import ChannelBinding

GUILD = 700
REPORT_CHANNEL = 701
DESTINATION = 702
MOSCOW = "Europe/Moscow"


class _EmptyChannel:
    """A report channel with no messages: these tests are about scheduling, not parsing."""

    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[str] = []
        self._next = 1

    def history(self, **kwargs: object) -> object:
        async def empty() -> object:
            return
            yield  # pragma: no cover

        return empty()

    async def send(self, content: str, **kwargs: object) -> object:
        self.sent.append(content)
        probe = _Probe(self._next)
        self._next += 1
        return probe


class _Probe:
    def __init__(self, probe_id: int) -> None:
        self.id = probe_id

    async def delete(self) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self._channels = {
            REPORT_CHANNEL: _EmptyChannel(REPORT_CHANNEL),
            DESTINATION: _EmptyChannel(DESTINATION),
        }

    def get_channel(self, channel_id: int) -> object:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> object:
        channel = self._channels.get(channel_id)
        if channel is None:
            raise RuntimeError("no such channel")
        return channel


@pytest.fixture
async def bound(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'close.db'}"))
    await db.create_all()
    await db.prepare()
    async with db.write_session() as session:
        await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
        await admin.bind_channel(
            session,
            guild_id=GUILD,
            channel_id=REPORT_CHANNEL,
            module_key="reports",
            schema_key="training_report",
            locale="ru",
        )
    yield db
    await db.dispose()


async def _binding(db: Database) -> ChannelBinding:
    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, REPORT_CHANNEL)
    assert binding is not None
    return binding


async def _arm(db: Database, *, close: datetime, destination: int | None = DESTINATION) -> None:
    binding = await _binding(db)
    async with db.write_session() as session:
        await repos.set_binding_schedule(
            session, binding_id=binding.id, next_close=close, period_days=7
        )
        if destination is not None:
            row = await repos.get_binding_by_id(session, binding.id)
            assert row is not None
            config = dict(row.config or {})
            config["report_channel_id"] = destination
            row.config = config


async def _next_close(db: Database) -> datetime:
    binding = await _binding(db)
    schedule = repos.binding_schedule(binding)
    assert schedule is not None
    return schedule.next_close


async def test_a_due_close_publishes_and_rolls_one_period(bound: Database) -> None:
    sink = RecordingReplySink()
    close_at = parse_close("22.11.2026 23:59:59", MOSCOW)
    await _arm(bound, close=close_at)
    binding = await _binding(bound)

    outcome = await close_module.close_period(
        db=bound,
        client=_Client(),
        binding_id=binding.id,
        now=close_at + timedelta(minutes=1),
        reply=sink,
    )

    assert outcome.action == "published"
    assert outcome.destination == DESTINATION
    assert sink.sent, "the report must actually be posted"
    assert await _next_close(bound) == close_at + timedelta(days=7)


async def test_a_failed_publish_does_not_advance_the_schedule(bound: Database) -> None:
    """The important ordering: publish first, move the clock second.

    If the schedule advanced on failure, the period would be silently lost: the bot would
    think that week was already reported.
    """
    close_at = parse_close("22.11.2026 23:59:59", MOSCOW)
    await _arm(bound, close=close_at, destination=None)
    binding = await _binding(bound)

    outcome = await close_module.close_period(
        db=bound,
        client=_Client(),
        binding_id=binding.id,
        now=close_at + timedelta(minutes=1),
        reply=RecordingReplySink(),
    )

    assert outcome.action == "failed"
    assert "report-target" in outcome.problem
    assert await _next_close(bound) == close_at


async def test_nothing_happens_before_the_close(bound: Database) -> None:
    close_at = parse_close("22.11.2026 23:59:59", MOSCOW)
    await _arm(bound, close=close_at)
    binding = await _binding(bound)
    sink = RecordingReplySink()

    outcome = await close_module.close_period(
        db=bound,
        client=_Client(),
        binding_id=binding.id,
        now=close_at - timedelta(hours=1),
        reply=sink,
    )

    assert outcome.action == "nothing"
    assert sink.sent == []
    assert await _next_close(bound) == close_at


async def test_a_close_missed_while_offline_is_skipped_and_advanced(bound: Database) -> None:
    """The stated policy: a stale week is not dumped into the channel on restart."""
    close_at = parse_close("22.11.2026 23:59:59", MOSCOW)
    await _arm(bound, close=close_at)
    binding = await _binding(bound)
    sink = RecordingReplySink()

    outcome = await close_module.close_period(
        db=bound,
        client=_Client(),
        binding_id=binding.id,
        now=close_at + timedelta(days=2),
        reply=sink,
    )

    assert outcome.action == "skipped"
    assert sink.sent == [], "a missed close must not post a stale report"
    assert await _next_close(bound) > close_at + timedelta(days=2)


async def test_a_restart_just_after_the_boundary_still_publishes(bound: Database) -> None:
    """A redeploy at 00:00:40 must not cost a week."""
    close_at = parse_close("22.11.2026 23:59:59", MOSCOW)
    await _arm(bound, close=close_at)
    binding = await _binding(bound)
    sink = RecordingReplySink()

    outcome = await close_module.close_period(
        db=bound,
        client=_Client(),
        binding_id=binding.id,
        now=close_at + timedelta(minutes=2),
        reply=sink,
    )

    assert outcome.action == "published"
    assert sink.sent


async def test_re_running_a_period_edits_instead_of_duplicating(bound: Database) -> None:
    close_at = parse_close("22.11.2026 23:59:59", MOSCOW)
    await _arm(bound, close=close_at)
    binding = await _binding(bound)
    sink = RecordingReplySink()

    await close_module.close_period(
        db=bound, client=_Client(), binding_id=binding.id, force=True, reply=sink
    )
    posted = len(sink.sent)

    await close_module.close_period(
        db=bound, client=_Client(), binding_id=binding.id, force=True, reply=sink
    )

    assert len(sink.sent) == posted, "a re-run must not post a second copy"
    assert sink.edited, "the re-run must have edited the existing report"


async def test_a_channel_without_a_schedule_does_nothing(bound: Database) -> None:
    binding = await _binding(bound)
    outcome = await close_module.close_period(
        db=bound, client=_Client(), binding_id=binding.id, reply=RecordingReplySink()
    )
    assert outcome.action == "nothing"
    assert "no close schedule" in outcome.problem


async def test_an_unreachable_destination_fails_before_counting(bound: Database) -> None:
    """A missing permission must be caught before a minute of Discord requests.

    Reproduces a 403 "Missing Access" on the destination channel: the report was counted,
    then the send failed, and only the log knew. Now it refuses up front, names the
    missing permission, and does not advance the schedule.
    """
    close_at = parse_close("22.11.2026 23:59:59", MOSCOW)
    await _arm(bound, close=close_at)
    binding = await _binding(bound)
    sink = RecordingReplySink()

    class _Forbidden(_EmptyChannel):
        async def send(self, content: str, **kwargs: object) -> object:
            import discord

            raise discord.Forbidden(_fake(403), {"code": 50001, "message": "Missing Access"})

    class _BlindClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self._channels[DESTINATION] = _Forbidden(DESTINATION)

    outcome = await close_module.close_period(
        db=bound,
        client=_BlindClient(),
        binding_id=binding.id,
        now=close_at + timedelta(minutes=1),
        reply=sink,
    )

    assert outcome.action == "failed"
    assert "доступ" in outcome.problem, "the message must name the missing access"
    assert sink.sent == [], "nothing may be published into a channel it cannot write to"
    assert await _next_close(bound) == close_at, "a failed publish must not advance"


def _fake(status: int) -> Any:
    class _Response:
        def __init__(self) -> None:
            self.status = status
            self.reason = "Forbidden"

    return _Response()


async def test_a_failed_send_names_the_reason_not_just_nothing(bound: Database) -> None:
    """`nothing was posted` is not an error message.

    The live failure reported exactly that sentence, which sends an operator to the logs
    instead of to the channel permission. The sink's reason must reach the reply.
    """
    close_at = parse_close("22.11.2026 23:59:59", MOSCOW)
    await _arm(bound, close=close_at)
    binding = await _binding(bound)

    class _RefusingSink(RecordingReplySink):
        last_error = ""

        async def send(self, channel_id: int, content: str, **kwargs: object) -> None:
            self.last_error = f"нет доступа к <#{channel_id}>: нужны права"
            return None

    outcome = await close_module.close_period(
        db=bound,
        client=_Client(),
        binding_id=binding.id,
        now=close_at + timedelta(minutes=1),
        reply=_RefusingSink(),
    )

    assert outcome.action == "failed"
    assert "доступ" in outcome.problem, "the cause must reach the user, not just the log"
    assert "nothing was posted" not in outcome.problem
    assert await _next_close(bound) == close_at, "a failed publish must not advance"

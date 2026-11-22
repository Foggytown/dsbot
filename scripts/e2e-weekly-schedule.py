"""End-to-end: set a schedule, close the period, publish, roll forward.

Proves the weekly system without a gateway connection:

  1. no destination set -> refuses, and does NOT advance the schedule
  2. a due close -> publishes into the destination and rolls forward one period
  3. before the next close -> nothing happens
  4. a close missed while the bot was down -> skipped, not published late
  5. a forced re-run -> edits the same message instead of posting a duplicate

    uv run python scripts/e2e-weekly-schedule.py
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from datetime import timedelta

from dsbot.config import Settings
from dsbot.discord.reply import RecordingReplySink
from dsbot.modules.quotas import close as close_module
from dsbot.modules.quotas.schedule import describe_close, parse_close
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.storage import admin, repos
from dsbot.storage.db import Database

GUILD = 1274460259661910149
REPORT_CHANNEL = 1274460260941434901
DESTINATION = 1573045809774067812


class FakeChannel:
    """A report channel with no history."""

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


class FakeClient:
    def __init__(self) -> None:
        self._channels = {
            REPORT_CHANNEL: FakeChannel(REPORT_CHANNEL),
            DESTINATION: FakeChannel(DESTINATION),
        }

    def get_channel(self, channel_id: int) -> object:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> object:
        channel = self._channels.get(channel_id)
        if channel is None:
            raise RuntimeError("no such channel")
        return channel


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp / 'w.db'}"))
    await db.create_all()
    await db.prepare()
    sink = RecordingReplySink()
    client = FakeClient()

    try:
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
        async with db.session() as session:
            binding = await repos.get_binding_by_channel(session, REPORT_CHANNEL)
        assert binding is not None

        close_time = parse_close("22.11.2026 23:59:59", "Europe/Moscow")
        async with db.write_session() as session:
            await repos.set_binding_schedule(
                session, binding_id=binding.id, next_close=close_time, period_days=7
            )

        # ---- 1. no destination -------------------------------------------
        outcome = await close_module.close_period(
            db=db,
            client=client,
            binding_id=binding.id,
            now=close_time + timedelta(minutes=1),
            reply=sink,
        )
        print(f"1. no destination      -> {outcome.action}")
        print(f"   {outcome.problem}")
        assert outcome.action == "failed"
        async with db.session() as session:
            fresh = await repos.get_binding_by_id(session, binding.id)
        assert repos.binding_schedule(fresh).next_close == close_time
        print("   schedule NOT advanced: correct\n")

        # ---- 2. due close -------------------------------------------------
        async with db.write_session() as session:
            row = await repos.get_binding_by_id(session, binding.id)
            config = dict(row.config or {})
            config["report_channel_id"] = DESTINATION
            row.config = config

        outcome = await close_module.close_period(
            db=db,
            client=client,
            binding_id=binding.id,
            now=close_time + timedelta(minutes=1),
            reply=sink,
        )
        print(f"2. due close           -> {outcome.action}  period={outcome.period_label}")
        print(f"   pages={outcome.pages} destination={outcome.destination}")
        assert outcome.action == "published"
        async with db.session() as session:
            fresh = await repos.get_binding_by_id(session, binding.id)
        rolled = repos.binding_schedule(fresh).next_close
        print(f"   next close: {describe_close(rolled, 'Europe/Moscow')}")
        assert rolled == close_time + timedelta(days=7)
        print("   rolled forward exactly 7 days: correct\n")

        # ---- 3. not due ---------------------------------------------------
        quiet = await close_module.close_period(
            db=db,
            client=client,
            binding_id=binding.id,
            now=close_time + timedelta(days=2),
            reply=sink,
        )
        print(f"3. two days later      -> {quiet.action}")
        assert quiet.action == "nothing"
        print()

        # ---- 4. missed close ----------------------------------------------
        missed = rolled + timedelta(days=3)
        skipped = await close_module.close_period(
            db=db,
            client=client,
            binding_id=binding.id,
            now=missed,
            reply=sink,
        )
        print(f"4. bot down 3 days     -> {skipped.action}")
        print(f"   {skipped.problem}")
        assert skipped.action == "skipped"
        async with db.session() as session:
            fresh = await repos.get_binding_by_id(session, binding.id)
        after = repos.binding_schedule(fresh).next_close
        assert after > missed
        print(f"   advanced to {describe_close(after, 'Europe/Moscow')}: correct\n")

        # ---- 5. a forced re-run of the same period edits, never duplicates ---
        first = await close_module.close_period(
            db=db, client=client, binding_id=binding.id, force=True, reply=sink
        )
        posted_after_first = len(sink.sent)
        print(f"5. forced run          -> {first.action}  period={first.period_label}")
        print(f"   messages posted: {posted_after_first}")

        again = await close_module.close_period(
            db=db, client=client, binding_id=binding.id, force=True, reply=sink
        )
        print(f"6. forced run AGAIN    -> {again.action}")
        added = len(sink.sent) - posted_after_first
        print(f"   new messages: {added}  edits: {len(sink.edited)}")
        assert again.action == "published"
        assert added == 0, "a re-run must edit the existing report, not post another"
        assert sink.edited, "the re-run must have edited something"
        print("   edited in place, nothing duplicated: correct")

        print()
        print("OK: schedules publish, roll forward, skip missed closes, re-run by editing")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

"""Verifies the whole Discord-side setup path, without a live gateway.

Reproduces what `/dsbot roster-refresh` does: read members, match them to the quota
ladder, store the roster, and confirm a count then finds people.

    uv run python scripts/e2e-roster-discord.py
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from datetime import UTC, datetime

from dsbot.config import Settings
from dsbot.modules.quotas.period import Period
from dsbot.modules.quotas.service import CountingService, CountRequest
from dsbot.modules.ranks.ladder import load_ladder
from dsbot.modules.ranks.roster import RosterService, StaticRosterSource
from dsbot.modules.reports.schema import FormSchemaDef
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.storage import admin, ranks_repo
from dsbot.storage.db import Database

GUILD = 1274402776696488108
CHANNEL = 1274460260941434901


class Member:
    def __init__(self, uid: int, roles: list[int], name: str) -> None:
        self.id = uid
        self.roles = [type("R", (), {"id": r})() for r in roles]
        self.display_name = name


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'r.db').as_posix()}"))
    try:
        await db.prepare()
        schema: FormSchemaDef = parse_form_file(pathlib.Path("forms/training.yaml"))

        async with db.write_session() as session:
            await admin.ensure_schema_seed(session, [schema])
            binding = await admin.bind_channel(
                session,
                guild_id=1274460259661910149,
                channel_id=CHANNEL,
                module_key="reports",
                schema_key=schema.key,
                rank_source_guild_id=GUILD,
            )
            for name in ("review", "quota"):
                await ranks_repo.replace_ladder(
                    session, load_ladder(pathlib.Path(f"forms/ladders/{name}.yaml"))
                )

        async with db.session() as session:
            group = (await ranks_repo.load_all_ladders(session))[GUILD]

        review_roles = sum(
            1
            for index in group.indexes
            if index.purpose == "review"
            for entry in index.by_role.values()
            if "review" in entry[3]
        )
        print(f"review roles loaded: {review_roles}")
        assert review_roles > 0, "review authority would be unreachable"

        # Two real role ids from the ladders.
        quota_roles = list(group.indexes[1].by_role) if len(group.indexes) > 1 else []
        senior_role = next(
            (r for r, e in group.indexes[0].by_role.items() if e[0] == "senior_instructor"),
            None,
        )
        members = [
            Member(1001, [quota_roles[0]] if quota_roles else [], "Tracker One"),
            Member(1002, [senior_role] if senior_role else [], "Senior Person"),
            Member(1003, [], "No Roles"),
        ]

        service = RosterService(db, StaticRosterSource({GUILD: members}))
        result = await service.refresh(GUILD, group, ladder_id=group.quota_ladder_id or 0)
        print(f"roster refreshed: {len(result.members)} of {result.total_members} matched")
        assert result.ok

        period = Period(
            start=datetime(2026, 11, 16, tzinfo=UTC), end=datetime(2026, 11, 23, tzinfo=UTC)
        )
        outcome = await CountingService(db).count(
            CountRequest(binding_id=binding.id, channel_id=CHANNEL, period=period)
        )
        print(f"count sees {len(outcome.summary.everyone())} person(s)")
        for page in outcome.messages():
            print(page)
        assert outcome.summary.everyone(), "the count must have somebody to report on"

        print("\nOK: roster-refresh populates the roster and the count uses it")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

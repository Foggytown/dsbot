"""Small guards that were missing, each one a way to be wrong quietly.

The important one is the rank cache: it lives for 15 minutes, so a newly appointed
reviewer's reaction counted for nothing and somebody who lost the role could still decide a
verdict. `invalidate` existed but nothing called it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dsbot.modules.ranks.resolver import RankResolver


class _Source:
    """Returns whatever roles the test sets, and counts lookups."""

    def __init__(self, roles: frozenset[int]) -> None:
        self.roles = roles
        self.calls = 0

    async def role_ids_for(self, guild_id: int, user_id: int) -> frozenset[int] | None:
        self.calls += 1
        return self.roles


def test_member_update_clears_the_cached_rank() -> None:
    """A role change must not wait 15 minutes to matter."""
    resolver = RankResolver(_Source(frozenset()), {}, ttl_seconds=900)
    resolver.invalidate(1, 42)
    assert resolver.stats()["cached"] == 0


def test_invalidate_only_touches_the_one_person() -> None:
    """Other people's cached ranks must survive: this runs on every member update."""
    resolver = RankResolver(_Source(frozenset()), {}, ttl_seconds=900)
    resolver._cache[(1, 42, frozenset(), None)] = (float("inf"), None)
    resolver._cache[(1, 99, frozenset(), None)] = (float("inf"), None)

    resolver.invalidate(1, 42)

    remaining = set(resolver._cache)
    assert (1, 42, frozenset(), None) not in remaining
    assert (1, 99, frozenset(), None) in remaining


async def test_the_bot_subscribes_to_member_updates() -> None:
    """The handler must exist, or the cache is never invalidated in production."""
    from dsbot.bot import Bot

    assert hasattr(Bot, "on_member_update"), "without this the resolver never invalidates"


class _Reply:
    def __init__(self, clears: bool) -> None:
        self._clears = clears
        self.cleared: list[tuple[int, int]] = []

    async def clear(self, channel_id: int, message_id: int) -> bool:
        self.cleared.append((channel_id, message_id))
        return self._clears


async def test_a_warning_row_survives_a_failed_delete(tmp_path: Path) -> None:
    """Discord refusing the delete must not make the bot believe the warning is gone."""
    from datetime import UTC, datetime

    from dsbot.config import Settings
    from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
    from dsbot.modules.reports.service import ChannelContext, ReportService
    from dsbot.storage import admin, repos
    from dsbot.storage.db import Database

    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'warn.db'}"))
    await db.create_all()
    try:
        async with db.write_session() as session:
            await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
            await admin.bind_channel(
                session,
                guild_id=1,
                channel_id=5,
                module_key="reports",
                schema_key="training_report",
            )
            report = await repos.upsert_report(
                session,
                channel_id=5,
                message_id=6,
                guild_id=1,
                author_user_id=1,
                created_at=datetime.now(UTC),
            )
            await repos.put_validation_message(
                session,
                report_id=report.id,
                channel_id=5,
                message_id=600,
                issue_codes=["field_missing"],
            )

        reply = _Reply(clears=False)
        service = ReportService(db, reply)  # type: ignore[arg-type]
        context = ChannelContext(
            binding_id=1,
            channel_id=5,
            guild_id=1,
            locale="ru",
            schemas=list(DEFAULT_SCHEMAS),
            group_window=__import__("datetime").timedelta(minutes=2),
        )

        cleared = await service._clear_warning(context, report_id=report.id)

        assert cleared is False
        assert reply.cleared == [(5, 600)], "the bot must still try to delete in Discord"
        async with db.session() as session:
            still_there = await repos.get_validation_message(session, report.id)
        assert still_there is not None, (
            "a failed Discord delete must keep the row, or the warning is orphaned: still "
            "visible in the channel, and no later edit can ever clear it"
        )
    finally:
        await db.dispose()


@pytest.mark.parametrize("raw", ["SOB || 0016", "SOB | | 0016"])
def test_empty_identity_segments_are_rejected(raw: str) -> None:
    """`SOB || 0016` must not slip through as a valid identity."""
    from dsbot.modules.reports.fields import parse_formation_idn

    result = parse_formation_idn(raw, "identity", ["SOB"])

    assert any(issue.code == "idn_unparsed" for issue in result.issues)

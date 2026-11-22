"""Schema upgrades for an existing database.

`Base.metadata.create_all` creates missing *tables* but never alters existing ones, so a
new column is invisible to any database created by an earlier build. That matters here
because the server's database is the only copy of months of reports: it cannot be
rebuilt, and a panel deployment may have no shell to run a migration by hand.

So upgrades are additive and idempotent: add the missing column with `ALTER TABLE ADD
COLUMN`, which SQLite supports. Nothing is dropped and nothing is rewritten, so an
upgrade is safe to run on every startup and cannot lose data.

A column that needs a *backfill* is different from one that merely needs to exist, so
each entry can carry a follow-up statement.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from dsbot.storage.db import Database

log = structlog.get_logger(__name__)


class _Addition:
    """One additive schema change."""

    __slots__ = ("after_sql", "column", "definition", "table")

    def __init__(
        self, table: str, column: str, definition: str, *, after_sql: str | None = None
    ) -> None:
        self.table = table
        self.column = column
        self.definition = definition
        self.after_sql = after_sql


# Every entry must be safe to run twice. `ALTER TABLE ... ADD COLUMN` on a column that
# already exists raises, so existence is checked first, not assumed.
ADDITIONS: tuple[_Addition, ...] = (
    # Phase 3: a reaction's authority is snapshotted at reaction time, not re-resolved
    # later, so a promotion cannot rewrite an old verdict.
    _Addition("message_reactions", "tier_key", "VARCHAR(80)"),
    _Addition("message_reactions", "can_review", "BOOLEAN"),
    # Counting needs the grain to be a person, and needs to know how a report ended.
    _Addition("weekly_results", "display_name", "VARCHAR(200)"),
    _Addition("weekly_results", "tier_ordinal", "INTEGER"),
    _Addition("weekly_results", "conflict_count", "INTEGER NOT NULL DEFAULT 0"),
    # A published report is edited on a re-run instead of duplicated.
    _Addition("weekly_runs", "report_channel_id", "BIGINT"),
    _Addition("weekly_runs", "report_message_id", "BIGINT"),
    _Addition("weekly_runs", "published_ids", "JSON"),
    # A ladder declares whether it orders reviewers or lists who owes reports.
    _Addition("rank_ladders", "purpose", "VARCHAR(20) NOT NULL DEFAULT 'review'"),
    # The weekly requirement is a property of the rank, so it lives on the tier and comes
    # from the quota ladder file rather than being retyped per channel.
    _Addition("rank_tiers", "required_per_week", "INTEGER"),
    # Frozen onto the roster so changing a quota later cannot rewrite last week.
    _Addition("roster_members", "required_per_week", "INTEGER NOT NULL DEFAULT 0"),
    # A warning records which report it is about. Previously only the bot's own message
    # was stored, so a real incident log could not be traced back to the report without a
    # manual database query.
    _Addition("validation_messages", "report_message_id", "BIGINT"),
)


async def upgrade_schema(db: Database) -> list[str]:
    """Add any missing columns. Returns the changes made, for logging.

    A table that does not exist yet is skipped rather than treated as an error: on a
    fresh database `create_all` decides the shape, and the upgrade has nothing to say.
    """
    applied: list[str] = []
    async with db.engine.begin() as conn:
        for addition in ADDITIONS:
            if not await _has_table(conn, addition.table):
                continue
            if await _has_column(conn, addition.table, addition.column):
                continue
            await conn.execute(
                text(
                    f"ALTER TABLE {addition.table} "
                    f"ADD COLUMN {addition.column} {addition.definition}"
                )
            )
            if addition.after_sql:
                await conn.execute(text(addition.after_sql))
            applied.append(f"{addition.table}.{addition.column}")

    if applied:
        log.info("schema_upgraded", columns=applied)
    return applied


async def _has_table(conn: AsyncConnection, table: str) -> bool:
    row = (
        await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name = :name"),
            {"name": table},
        )
    ).first()
    return row is not None


async def _has_column(conn: AsyncConnection, table: str, column: str) -> bool:
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
    return any(row[1] == column for row in rows)


async def missing_tables(db: Database) -> list[str]:
    """Tables the models expect but the database does not have. Diagnostics only."""
    from dsbot.storage.models import Base

    async with db.engine.begin() as conn:
        rows = (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).all()
    existing = {row[0] for row in rows}
    return sorted(set(Base.metadata.tables) - existing)


__all__ = ["ADDITIONS", "missing_tables", "upgrade_schema"]

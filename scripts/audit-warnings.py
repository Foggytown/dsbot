"""Which reports did the bot actually warn about, and were they old?

Read-only by construction: opens the database with `mode=ro`, so it cannot alter,
migrate or corrupt a copy of the live one. Safe to run against a file pulled off the
server.

    uv run python scripts/audit-warnings.py --db /path/to/dsbot.db
    uv run python scripts/audit-warnings.py --db dsbot.db --old-days 7

Why this exists: the `warning_posted` log line used to carry only the bot's own message
id, so a warning could not be traced back to the report it complained about from the log
alone. The database always had the link (`validation_messages.report_id ->
reports.message_id`), it just needed a query. This prints it.

It also separates two very different stories for each warning:

  * a `message_created` event near the report's creation time  -> the bot saw a new
    message and judged it. Normal.
  * only a `message_edited` event, or a create long after        -> the bot re-judged an
    existing message. This is the shape of the incident where it warned about
    half-year-old reports.

The report text is printed so the verdict can be checked by a human, not trusted.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Discord snowflakes encode their creation time. Decoding needs no API call, which is
# what made it possible to tell a bot message from a report in the incident log.
DISCORD_EPOCH_MS = 1420070400000


def snowflake_time(value: int | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(((int(value) >> 22) + DISCORD_EPOCH_MS) / 1000, UTC)
    except (ValueError, OSError, OverflowError):
        return None


def parse_ts(value: object) -> datetime | None:
    """SQLite stores ISO strings; tolerate the 'YYYY-MM-DD HH:MM:SS' form too."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _report_freshness(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Print how much this database actually knows.

    A copy taken while the bot is running, or taken from the wrong path, can be hours or
    days behind while still answering every query. Counting rows makes that visible
    instead of letting a stale file quietly define the answer.
    """

    def newest(table: str, column: str) -> str:
        if table not in tables:
            return "n/a"
        row = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
        return str(row[0]) if row and row[0] else "(empty)"

    def count(table: str) -> str:
        if table not in tables:
            return "n/a"
        return str(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    print("SNAPSHOT COVERAGE  (is this file current?)")
    for label, table, column in (
        ("messages", "messages", "created_at"),
        ("reports", "reports", None),
        ("raw_events", "raw_events", "received_at"),
        ("validation_msgs", "validation_messages", "created_at"),
        ("roster_members", "roster_members", None),
        ("weekly_runs", "weekly_runs", None),
    ):
        newest_value = newest(table, column) if column else ""
        suffix = f"   newest {newest_value}" if column else ""
        print(f"  {label:<17}: {count(table):>6}{suffix}")
    if "raw_events" in tables:
        latest = conn.execute("SELECT MAX(received_at) FROM raw_events").fetchone()[0]
        stamp = parse_ts(latest)
        if stamp:
            age = datetime.now(UTC) - stamp
            print(
                f"\n  newest event in this file: {stamp:%Y-%m-%d %H:%M:%S} UTC "
                f"({age.total_seconds() / 3600:.1f} h ago)"
            )
            if age > timedelta(hours=6):
                print(
                    "  WARNING: this snapshot is old. If the bot has been running since, this\n"
                    "  file is missing events and its warnings are NOT the full list. Copy\n"
                    "  dsbot.db from the server again, with the bot stopped or just started,\n"
                    "  and include dsbot.db-wal if it is not empty."
                )
    print()


def audit(db_path: Path, old_days: float, limit: int) -> int:
    if not db_path.exists():
        print(f"no such file: {db_path}")
        return 2

    # mode=ro: the whole point is to never write to something pulled off the server.
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        print(f"database: {db_path}")
        print("read-only: yes (mode=ro)\n")

        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for needed in ("reports", "validation_messages"):
            if needed not in tables:
                print(f"missing table: {needed} - is this a dsbot database?")
                return 2

        has_new_column = "report_message_id" in columns(conn, "validation_messages")
        print(
            "validation_messages.report_message_id: "
            + ("present (post-fix database)" if has_new_column else "absent (pre-fix database)")
        )
        print("  A pre-fix database still answers this: join through reports.message_id.\n")

        # Is this file even current? A stale copy answers confidently about the wrong
        # period, and that is worse than refusing: the numbers look authoritative.
        _report_freshness(conn, tables)

        # One row per warning still recorded. A warning row is deleted once the report is
        # fixed, so a cleared warning leaves no row here - but the report stays in
        # `reports` as 'invalid', which the second query covers.
        report_col = "v.report_message_id" if has_new_column else "r.message_id"
        query = f"""
            SELECT
                v.id                AS warning_row,
                v.message_id        AS warning_message_id,
                v.created_at        AS warned_at,
                v.issue_codes       AS issue_codes,
                r.id                AS report_id,
                {report_col}        AS report_message_id,
                r.status            AS report_status,
                r.author_user_id    AS author_id,
                m.created_at        AS message_created,
                m.content           AS content,
                m.author_name       AS author_name,
                m.edited_at         AS edited_at
            FROM validation_messages v
            JOIN reports r ON r.id = v.report_id
            LEFT JOIN messages m
                   ON m.channel_id = r.channel_id AND m.message_id = r.message_id
            ORDER BY v.created_at DESC
            LIMIT ?
        """
        rows = conn.execute(query, (limit,)).fetchall()

        print(f"warnings currently on record: {len(rows)}\n")
        if not rows:
            print("Nothing to audit: every warning has been cleared or none was posted.")

        old: list[sqlite3.Row] = []
        for row in rows:
            created = parse_ts(row["message_created"]) or snowflake_time(row["report_message_id"])
            warned = parse_ts(row["warned_at"])
            age = (warned - created) if (created and warned) else None
            age_hours = age.total_seconds() / 3600 if age else None
            if age_hours is not None and age_hours < 0:
                age_hours = 0.0  # clock skew between received_at and created_at

            if age_hours is not None and age_hours > old_days * 24:
                old.append(row)

            print("-" * 78)
            print(f"report {row['report_id']}  ({row['report_status']})")
            print(f"  warning message : {row['warning_message_id']}")
            print(f"  report message  : {row['report_message_id']}")
            print(f"  author          : {row['author_name'] or '?'} ({row['author_id']})")
            if created:
                print(f"  message created : {created:%Y-%m-%d %H:%M:%S} UTC")
            if warned:
                print(f"  warned at       : {warned:%Y-%m-%d %H:%M:%S} UTC")
            if age_hours is not None:
                flag = "  <-- OLD" if age_hours > old_days * 24 else ""
                print(f"  age at warning  : {age_hours:.1f} h{flag}")
            print(f"  issues          : {row['issue_codes']}")
            if row["edited_at"]:
                print(f"  edited at       : {parse_ts(row['edited_at'])}")
            text = (row["content"] or "").strip().replace("\n", "\n                    ")
            print(f"  text            : {text or '(no stored copy)'}")

        # Did the bot *see* this message arrive, or only see it change? That is the
        # difference between judging a new report and re-judging an old one.
        print()
        print("=" * 78)
        print("HOW EACH MESSAGE REACHED THE BOT (raw event log)")
        print("=" * 78)
        if "raw_events" not in tables:
            print("no raw_events table; cannot trace the trigger")
        else:
            for row in rows:
                message_id = row["report_message_id"]
                events = conn.execute(
                    """
                    SELECT kind, received_at, source
                    FROM raw_events
                    WHERE message_id = ?
                    ORDER BY received_at
                    """,
                    (message_id,),
                ).fetchall()
                kinds = [f"{e['kind']}@{parse_ts(e['received_at']):%m-%d %H:%M:%S}" for e in events]
                verdict = _trigger_verdict(events)
                print(f"\nreport {row['report_id']} (message {message_id}): {verdict}")
                for line in kinds:
                    print(f"    {line}")

        print()
        print("=" * 78)
        if old:
            print(
                f"OLD WARNINGS: {len(old)} of {len(rows)} were about a report older than "
                f"{old_days:g} day(s)"
            )
            ids = ", ".join(str(r["report_message_id"]) for r in old)
            print(f"  report message ids: {ids}")
            print("  These are the ids to check in Discord.")
        else:
            print(f"No warning was about a report older than {old_days:g} day(s).")
        print()
        print(
            "Note: a warning row is deleted when the report is later fixed, so a cleared\n"
            "warning is absent here. Reports still carrying status='invalid' remain in the\n"
            "`reports` table even then."
        )
        return 0
    finally:
        conn.close()


def _trigger_verdict(events: list[sqlite3.Row]) -> str:
    """Classify how the message reached the pipeline."""
    if not events:
        return "no event recorded (stored by an earlier build or a backfill)"
    kinds = [event["kind"] for event in events]
    if "message_created" in kinds:
        created = [event for event in events if event["kind"] == "message_created"]
        first = parse_ts(created[0]["received_at"])
        if first is None:
            return "new message"
        return f"appeared as a NEW message at {first:%Y-%m-%d %H:%M:%S} UTC"
    if "message_edited" in kinds:
        return "ONLY seen as an EDIT - never arrived as a new message (re-judged)"
    return f"only: {', '.join(sorted(set(kinds)))}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to dsbot.db (opened read-only)")
    parser.add_argument(
        "--old-days",
        type=float,
        default=1.0,
        help="Flag warnings about a report older than this many days (default 1)",
    )
    parser.add_argument("--limit", type=int, default=200, help="How many warnings to list")
    args = parser.parse_args()
    raise SystemExit(audit(Path(args.db), args.old_days, args.limit))


if __name__ == "__main__":
    main()

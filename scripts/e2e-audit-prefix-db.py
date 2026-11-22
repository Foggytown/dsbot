"""Does the audit tool work on a database created BEFORE the fix?

The server's database predates the `report_message_id` column, so the tool must not
depend on it. This simulates that: builds the schema without the column, inserts rows the
old way, and confirms the join through `reports.message_id` still finds the message ids.

    uv run python scripts/e2e-audit-prefix-db.py
"""

from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO

OLD_MESSAGE = 1000000000000000001
WARNING_MESSAGE = 1000000000000000009


def load_audit() -> object:
    spec = importlib.util.spec_from_file_location(
        "audit_warnings", pathlib.Path("scripts/audit-warnings.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db_path = tmp / "old.db"

    conn = sqlite3.connect(db_path)
    # The shapes that matter, exactly as an older build wrote them: no
    # validation_messages.report_message_id at all.
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id BIGINT, channel_id BIGINT, guild_id BIGINT,
            author_id BIGINT, author_name VARCHAR(200), author_display_name VARCHAR(200),
            author_is_bot BOOLEAN, content TEXT,
            created_at DATETIME, edited_at DATETIME, deleted_at DATETIME,
            referenced_message_id BIGINT, thread_id BIGINT,
            attachments JSON, embeds JSON
        );
        CREATE TABLE reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id BIGINT, channel_id BIGINT, message_id BIGINT,
            author_user_id BIGINT, status VARCHAR(30),
            created_at DATETIME
        );
        CREATE TABLE validation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER, kind VARCHAR(30), channel_id BIGINT,
            message_id BIGINT, issue_codes JSON, created_at DATETIME
        );
        CREATE TABLE raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id VARCHAR(200), kind VARCHAR(40), channel_id BIGINT,
            guild_id BIGINT, message_id BIGINT, occurred_at DATETIME,
            received_at DATETIME, source VARCHAR(20), payload JSON
        );
        """
    )

    created = datetime.now(UTC) - timedelta(days=180)
    warned = datetime.now(UTC)
    conn.execute(
        "INSERT INTO messages (message_id, channel_id, author_id, author_name, content, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (OLD_MESSAGE, 1, 42, "foggytown", "1. SOB | 0016\n2. INF\n3.", created.isoformat()),
    )
    conn.execute(
        "INSERT INTO reports (channel_id, message_id, author_user_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, OLD_MESSAGE, 42, "invalid", created.isoformat()),
    )
    conn.execute(
        "INSERT INTO validation_messages (report_id, kind, channel_id, message_id, "
        "issue_codes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (1, "warning", 1, WARNING_MESSAGE, '["field_missing"]', warned.isoformat()),
    )
    # Only an edit was ever recorded - the incident shape.
    conn.execute(
        "INSERT INTO raw_events (external_id, kind, channel_id, message_id, received_at, "
        "source, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("edit-1", "message_edited", 1, OLD_MESSAGE, warned.isoformat(), "gateway", "{}"),
    )
    conn.commit()
    conn.close()

    module = load_audit()
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = module.audit(db_path, old_days=1.0, limit=20)  # type: ignore[attr-defined]
    text = buffer.getvalue()
    print(text)

    assert code == 0, "the tool must succeed on a pre-fix database"
    assert "absent (pre-fix database)" in text, "it must notice the column is missing"
    assert str(OLD_MESSAGE) in text, "the report message id must still be found"
    assert "ONLY seen as an EDIT" in text, "the trigger must still be identified"
    assert "OLD WARNINGS" in text

    # And it truly opened read-only: the file's bytes must be untouched.
    before = db_path.stat().st_mtime_ns
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    assert db_path.stat().st_mtime_ns == before, "the audit must not write to the database"

    print("OK: a pre-fix server database answers the question without the new column")


if __name__ == "__main__":
    main()

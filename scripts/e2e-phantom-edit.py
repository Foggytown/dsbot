"""Replays the real MESSAGE_UPDATE payloads that caused the incident.

These are the actual `raw_events` rows from the server database, so this is not a
synthetic stand-in: it is the exact input that made the bot warn about reports from 2025.

    uv run python scripts/e2e-phantom-edit.py [path/to/dsbot.db]

With no path it uses the captured payloads below, so the check runs anywhere.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

from dsbot.bot import _edit_changed_message

# Captured from dsbot.db on the live server: the seven phantom edits that produced
# warnings about 2025 reports, plus one genuine edit for contrast.
CAPTURED = [
    # (message_id, payload, should_process)
    (
        1405162864922071180,
        {"content": "1. SOB | RC-77\n2. INF\n3. https://discord.com/...", "edited_at": None},
        False,
    ),
    (
        1348242991407562822,
        {"content": "1. SOB | RC-77, 666th | 4285\n2.T\n3.\n4.", "edited_at": None},
        False,
    ),
    (
        1371206842331435191,
        {"content": "1. SOB | RC-77\n2. T\n3. -\n4.", "edited_at": None},
        False,
    ),
    (
        1404900338577703003,
        {"content": "1. SOB | RC-77\n2. T\n3.", "edited_at": None},
        False,
    ),
    (
        1398789226195320832,
        {"content": "1. SOB | RC-77 \n2. T (MED снял)\n3.", "edited_at": None},
        False,
    ),
    (
        1348984229001953282,
        {"content": "1. SOB | RC-77\n2. INF\n3.\n4. https://discord.com/...", "edited_at": None},
        False,
    ),
    (
        1375830066076061776,
        {"content": "1. SOB | RC-71\n2. T\n3. \n4. -", "edited_at": None},
        False,
    ),
    (
        1573991238418571325,
        {
            "content": "https://discord.com/channels/.../1573991761359937561",
            "edited_at": "2026-11-22T09:21:37.136000+00:00",
        },
        True,
    ),
]


class Payload:
    """Just enough of RawMessageUpdateEvent for the guard."""

    def __init__(self, data: dict, cached: object = None) -> None:
        self.data = data
        self.cached_message = cached


def check_captured() -> int:
    print("CAPTURED PAYLOADS FROM THE INCIDENT")
    print("=" * 70)
    failures = 0
    for message_id, data, should_process in CAPTURED:
        got = _edit_changed_message(Payload(data))
        marker = "process" if got else "skip   "
        expected = "process" if should_process else "skip   "
        verdict = "ok" if got == should_process else "WRONG"
        if got != should_process:
            failures += 1
        edited = "set" if data.get("edited_at") else "None"
        print(f"  {message_id}  edited_at={edited:<5} -> {marker} (expect {expected}) {verdict}")
    return failures


def check_database(path: pathlib.Path) -> int:
    """Run every stored message_edited payload through the guard."""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT message_id, payload FROM raw_events WHERE kind='message_edited'"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("\nno message_edited rows in that database")
        return 0

    print(f"\nEVERY EDIT EVENT IN {path.name} ({len(rows)} rows)")
    print("=" * 70)
    skipped = processed = 0
    for row in rows:
        payload = json.loads(row["payload"])
        data = payload.get("message") or {}
        if _edit_changed_message(Payload(data)):
            processed += 1
            print(f"  {row['message_id']}  PROCESS  edited_at={data.get('edited_at')}")
        else:
            skipped += 1
            print(f"  {row['message_id']}  skip     edited_at={data.get('edited_at')}")
    print(f"\n  processed: {processed}   skipped: {skipped}")
    return 0


def main() -> None:
    failures = check_captured()
    if len(sys.argv) > 1:
        failures += check_database(pathlib.Path(sys.argv[1]))

    print()
    if failures:
        raise SystemExit(f"FAILED: {failures} payload(s) handled wrongly")
    print("OK: every phantom update is skipped and a real edit still gets through")


if __name__ == "__main__":
    main()

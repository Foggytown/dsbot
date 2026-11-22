"""The edit guard must read the field Discord actually sends.

`on_raw_message_edit` receives Discord's raw gateway payload, where the marker of a real
edit is `edited_timestamp`. Our own `MessageSnapshot` dataclass calls the same idea
`edited_at`, and an earlier version of the guard checked *that* name against the raw dict.
It never matched, so every genuine edit was treated as a phantom server-side update: the
handler returned before re-parsing, and a report warning never updated after the author
fixed their message.

    uv run python scripts/e2e-phantom-edit.py [path/to/dsbot.db]
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

from dsbot.bot import _edit_changed_message


class _Cached:
    def __init__(self, content: str) -> None:
        self.content = content


class _Payload:
    """Just enough of RawMessageUpdateEvent for the guard."""

    def __init__(self, data: dict, cached: object = None) -> None:
        self.data = data
        self.cached_message = cached


# Real gateway payload shapes, as Discord documents MESSAGE_UPDATE.
REAL_EDIT = {
    "id": "1573991757002055723",
    "channel_id": "1274460260941434901",
    "content": "1. SOB | 0016\n2. INF\n3. фото\n4. https://discord.com/channels/1/2/3",
    "timestamp": "2026-11-22T12:00:00.000000+00:00",
    "edited_timestamp": "2026-11-22T13:00:00.000000+00:00",
}

PHANTOM = {
    "id": "1405162864922071180",
    "channel_id": "1274460260941434901",
    "content": "1. SOB | RC-77\n2. INF\n3. https://discord.com/...",
    "timestamp": "2025-08-13T12:15:33.661000+00:00",
    # No edited_timestamp: an unfurl, a pin, a preview. Not a person editing.
}


def check_wire_shapes() -> int:
    print("THE GUARD AGAINST REAL GATEWAY PAYLOADS")
    print("=" * 74)
    failures = 0

    cases = [
        ("a person edited the text", REAL_EDIT, None, True),
        ("phantom: no edited_timestamp", PHANTOM, None, False),
        (
            "edited_timestamp but identical content",
            REAL_EDIT,
            _Cached(REAL_EDIT["content"]),
            False,
        ),
        (
            "edited_timestamp and different content",
            REAL_EDIT,
            _Cached("1. SOB | 0016\n2. INF\n3. фото\n4. "),
            True,
        ),
    ]
    for label, data, cached, expected in cases:
        got = _edit_changed_message(_Payload(data, cached))
        verdict = "ok" if got == expected else "WRONG"
        if got != expected:
            failures += 1
        print(f"  {label:<38} -> {'process' if got else 'skip':<8} {verdict}")

    # The specific mistake: checking the wrong key name.
    wrong = dict(REAL_EDIT)
    wrong["edited_at"] = wrong.pop("edited_timestamp")
    if _edit_changed_message(_Payload(wrong)):
        print("  a payload keyed on 'edited_at' would be ACCEPTED - the guard is wrong")
        failures += 1
    else:
        print("  a payload keyed on 'edited_at' is rejected, as it must be  ok")
    return failures


def check_stored(path: pathlib.Path) -> int:
    """Report what the archive holds. Informational: those are our own snapshots."""
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

    real = phantom = 0
    for row in rows:
        message = (json.loads(row["payload"]) or {}).get("message") or {}
        if message.get("edited_at"):
            real += 1
        else:
            phantom += 1

    print(f"\nARCHIVE: {len(rows)} edit events in {path.name}")
    print("=" * 74)
    print(f"  with a real edited_at : {real}")
    print(f"  without (phantom)     : {phantom}")
    print(
        "\n  Note: these stored payloads are our own MessageSnapshot, which spells the\n"
        "  field `edited_at`. Discord's wire format spells it `edited_timestamp`. The\n"
        "  guard reads the wire format because that is what the gateway hands us."
    )
    return 0


def main() -> None:
    failures = check_wire_shapes()
    if len(sys.argv) > 1:
        failures += check_stored(pathlib.Path(sys.argv[1]))

    print()
    if failures:
        raise SystemExit(f"FAILED: {failures} case(s) handled wrongly")
    print("OK: a real edit is processed and a phantom update is skipped")


if __name__ == "__main__":
    main()

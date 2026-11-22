"""When were the attachment URLs in the phantom-updated messages signed?

Discord signs attachment CDN URLs with `is` (issued) and `ex` (expires), both unix
seconds in hex. The issued time is when the message was last serialised for a client, so
comparing it with the arrival time of the MESSAGE_UPDATE says whether the bot or somebody
else caused the re-serialisation.

Read-only. Point it at any dsbot database.

    uv run python scripts/inspect-attachment-signing.py <path to dsbot.db>
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import urllib.parse as urlparse
from datetime import UTC, datetime


def signed(url: str) -> tuple[datetime | None, datetime | None]:
    query = urlparse.parse_qs(urlparse.urlparse(url).query)
    try:
        issued = datetime.fromtimestamp(int(query["is"][0], 16), UTC)
        expires = datetime.fromtimestamp(int(query["ex"][0], 16), UTC)
    except (KeyError, ValueError):
        return None, None
    return issued, expires


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: inspect-attachment-signing.py <dsbot.db>")
    path = pathlib.Path(sys.argv[1])
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print("EVENT SOURCES")
        print("=" * 60)
        for row in conn.execute(
            "SELECT source, kind, COUNT(*) AS n FROM raw_events "
            "GROUP BY source, kind ORDER BY source, kind"
        ):
            print(f"  source={row['source']:<10} kind={row['kind']:<18} {row['n']}")

        print()
        print("ATTACHMENT URL SIGNED -> EDIT ARRIVED")
        print("=" * 88)
        print(f"{'message id':<21}{'signed':<18}{'arrived':<20}{'gap':>8}")
        print("-" * 88)
        for row in conn.execute(
            "SELECT message_id, received_at, payload FROM raw_events "
            "WHERE kind='message_edited' ORDER BY received_at"
        ):
            message = (json.loads(row["payload"]) or {}).get("message") or {}
            attachments = message.get("attachments") or []
            if not attachments:
                continue
            issued, _ = signed(attachments[0]["url"])
            if issued is None:
                continue
            arrived = datetime.fromisoformat(str(row["received_at"])).replace(tzinfo=UTC)
            gap = (arrived - issued).total_seconds() / 3600
            print(
                f"{row['message_id']:<21}{issued:%Y-%m-%d %H:%M}  "
                f"{arrived:%Y-%m-%d %H:%M:%S}  {gap:+7.1f}h"
            )

        print()
        print("URL EXPIRIES (a URL is re-signed ~24h before it lapses)")
        print("=" * 88)
        for row in conn.execute(
            "SELECT message_id, payload FROM raw_events WHERE kind='message_edited'"
        ):
            message = (json.loads(row["payload"]) or {}).get("message") or {}
            attachments = message.get("attachments") or []
            if not attachments:
                continue
            issued, expires = signed(attachments[0]["url"])
            if issued is None:
                continue
            print(f"  {row['message_id']}: {issued:%m-%d %H:%M} -> {expires:%m-%d %H:%M}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

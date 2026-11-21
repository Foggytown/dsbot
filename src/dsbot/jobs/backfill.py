"""Backfill: pull channel history into the same shape as live events.

Two outputs, one walk:
  * JSONL dump (default) - portable, greppable, and the fixture source for parser tests
  * database            - `--to-db` writes through the live repos, idempotently

Backfill uses the same MessageSnapshot type as the gateway, so a backfilled week is
indistinguishable from live data downstream. That also makes a mid-week rollout work:
run it with `--since <monday>` and Sunday's evaluation counts the whole week.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import discord
import structlog

from dsbot.discord import sink
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class BackfillStats:
    messages: int = 0
    reactions: int = 0
    skipped_bots: int = 0
    errors: int = 0
    # Reports the parser recognised. Zero here with `--to-db` means the archive has
    # messages but the count will find nothing, which is the confusing case worth showing.
    parsed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "reactions": self.reactions,
            "skipped_bots": self.skipped_bots,
            "errors": self.errors,
            "parsed": self.parsed,
        }


def _message_record(message: discord.Message) -> dict[str, Any]:
    """A dump record: the snapshot plus reaction totals, enough to replay offline."""
    snapshot = sink.message_snapshot(message)
    payload = snapshot.to_payload()
    payload["reaction_summary"] = [
        {"emoji": sink.emoji_ref(r.emoji).to_payload(), "count": r.count} for r in message.reactions
    ]
    return payload


async def iter_channel_history(
    channel: discord.abc.Messageable,
    *,
    since: datetime | None = None,
    limit: int | None = None,
) -> AsyncIterator[discord.Message]:
    """Walk history newest-first, which is what the Discord API gives us cheaply."""
    kwargs: dict[str, Any] = {"limit": None if limit is None else limit}
    if since is not None:
        kwargs["after"] = since
        kwargs["oldest_first"] = True

    count = 0
    async for message in channel.history(**kwargs):  # type: ignore[attr-defined]
        yield message
        count += 1
        if limit is not None and count >= limit:
            return


async def _fetch_reactors(
    message: discord.Message, *, max_users_per_emoji: int = 1000
) -> AsyncIterator[tuple[str, int, int, int]]:
    """Yield (emoji_key, emoji_id, user_id, count) for a message's reactions.

    This is the expensive part of a backfill: one request per emoji per 100 users.
    Callers should treat it as best-effort, since a missing reaction only affects
    verdict history, never report content.
    """
    for reaction in message.reactions:
        ref = sink.emoji_ref(reaction.emoji)
        seen = 0
        try:
            async for user in reaction.users(limit=max_users_per_emoji):
                if getattr(user, "bot", False):
                    continue
                seen += 1
                yield ref.key, ref.id or 0, user.id, seen
        except discord.HTTPException:
            log.warning(
                "reaction_fetch_failed",
                message_id=message.id,
                emoji=ref.key,
            )
            continue


class JsonlWriter:
    """Streaming JSONL writer. One line per message, flushed per line for tailing."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: TextIO | None = None
        self._count = 0

    def __enter__(self) -> JsonlWriter:
        self._handle = self._path.open("w", encoding="utf-8", newline="\n")
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def write(self, record: dict[str, Any]) -> None:
        if self._handle is None:
            raise RuntimeError("writer is not open")
        self._handle.write(json.dumps(record, ensure_ascii=False, default=str))
        self._handle.write("\n")
        self._handle.flush()
        self._count += 1

    @property
    def count(self) -> int:
        return self._count


async def backfill_channel(
    channel: discord.abc.Messageable,
    *,
    since: datetime | None,
    limit: int | None,
    writer: JsonlWriter | None = None,
    db: Database | None = None,
    include_reactions: bool = True,
    include_bots: bool = False,
    progress_every: int = 250,
    parse: bool = False,
    report_service: Any = None,
) -> BackfillStats:
    """Walk one channel. JSONL and DB outputs can be produced in the same pass.

    `parse=True` also runs the report parser over every stored message. That is what makes
    a backfill countable: storing messages alone gives the archive but no reports, so a
    count over that period returns nothing. Parsing is opt-in because a JSONL-only dump
    does not need it, and re-parsing is expensive.
    """
    assert (writer is not None) or (db is not None), "need a destination"
    stats = BackfillStats()
    parsed_ids: list[tuple[int, int, int]] = []

    async for message in iter_channel_history(channel, since=since, limit=limit):
        if getattr(message.author, "bot", False) and not include_bots:
            stats.skipped_bots += 1
            continue

        record = _message_record(message)

        if include_reactions:
            reactors: list[dict[str, Any]] = []
            async for emoji_key, emoji_id, user_id, _count in _fetch_reactors(message):
                reactors.append({"emoji_key": emoji_key, "emoji_id": emoji_id, "user_id": user_id})
                stats.reactions += 1
            record["reactors"] = reactors

        if writer is not None:
            writer.write(record)

        if db is not None:
            snapshot = sink.message_snapshot(message)
            try:
                async with db.write_session() as session:
                    await repos.upsert_message(session, snapshot)
                    for reactor in record.get("reactors", []):
                        await repos.add_reaction(
                            session,
                            message_id=snapshot.message_id,
                            channel_id=snapshot.channel_id,
                            guild_id=snapshot.guild_id,
                            emoji_key=reactor["emoji_key"],
                            emoji_id=reactor["emoji_id"] or None,
                            emoji_name=None,
                            user_id=reactor["user_id"],
                        )
            except Exception:
                stats.errors += 1
                log.exception("backfill_message_failed", message_id=message.id)
            else:
                parsed_ids.append((snapshot.channel_id, snapshot.author_id, snapshot.message_id))

        stats.messages += 1
        if progress_every and stats.messages % progress_every == 0:
            log.info(
                "backfill_progress",
                messages=stats.messages,
                reactions=stats.reactions,
                channel_id=getattr(channel, "id", None),
            )

    if parse and report_service is not None:
        stats.parsed = await _parse_backfilled(report_service, parsed_ids)

    log.info(
        "backfill_channel_done",
        channel_id=getattr(channel, "id", None),
        **stats.as_dict(),
    )
    return stats


async def _parse_backfilled(report_service: Any, messages: list[tuple[int, int, int]]) -> int:
    """Run the report parser over backfilled messages.

    Live ingest parses as messages arrive; a backfill writes straight to the archive and
    would otherwise leave the reports table empty. Order matters for reporting: oldest
    first, so a report is grouped with its own follow-up photos and a later edit cannot be
    applied before the message it edits.
    """
    parsed = 0
    for channel_id, author_id, message_id in messages:
        try:
            result = await report_service.process_message(channel_id, author_id, message_id)
        except Exception:
            log.exception("backfill_parse_failed", message_id=message_id)
            continue
        if result is not None and result.matched:
            parsed += 1
    return parsed


def parse_since(value: str | None) -> datetime | None:
    """Accept an ISO date or datetime, or a relative duration like '14d'."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None

    if value[-1] in "dhm" and value[:-1].isdigit():
        amount = int(value[:-1])
        unit = value[-1]
        seconds = {"d": 86400, "h": 3600, "m": 60}[unit] * amount
        return datetime.fromtimestamp(datetime.now(UTC).timestamp() - seconds, tz=UTC)

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

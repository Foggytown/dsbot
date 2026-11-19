"""Idempotent ingest: persist the raw event, archive the message, then publish.

Handlers downstream are pure consumers. Persisting first means a crash mid-dispatch
loses no data: the raw log is the source of truth and replay can rebuild projections.
"""

from __future__ import annotations

import structlog

from dsbot.core.bus import EventBus
from dsbot.core.events import (
    Event,
)
from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)


class Ingestor:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus

    async def handle(self, event: Event, *, source: str = "gateway") -> None:
        """Store and forward one event. Safe to call twice with the same event."""
        new = await self._db.run_write(
            lambda session: repos.append_raw_event(session, event, source=source)
        )
        if not new:
            log.debug("duplicate_event_skipped", kind=event.kind, id=event.external_id)
            return
        await self._bus.publish(event)

    async def ingest_message(self, snapshot: object) -> None:
        """Archive a message snapshot. Used by live ingest and backfill alike."""
        async with self._db.write_session() as session:
            await repos.upsert_message(session, snapshot)

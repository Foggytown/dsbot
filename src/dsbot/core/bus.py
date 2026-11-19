"""Ordered, per-channel event dispatch.

Reports are order-sensitive: an edit must be applied after the message it edits, and
a reaction after the message it reacts to. Discord delivers gateway events per-guild
in order, but backfill and live ingest run concurrently, so ordering is enforced here
rather than assumed.

Design: one asyncio task per channel, each draining a FIFO queue. Unbound channels
still get their own lane so a slow handler cannot starve an unrelated channel.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from dsbot.core.events import Event, event_channel_id, event_external_id

log = structlog.get_logger(__name__)

Handler = Callable[[Any], Awaitable[None]]

_DEFAULT_QUEUE_SIZE = 2048


class EventBus:
    """Fan-out dispatcher with per-channel FIFO ordering.

    Subscribers are invoked sequentially inside a channel lane, in subscription
    order. A raising handler is logged and does not stop the lane, because a single
    malformed message must never take the bot offline.
    """

    def __init__(self, queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[str, list[Handler]] = {}
        self._lanes: dict[int, asyncio.Queue[Event]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._closed = False
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self.processed = 0
        self.failed = 0

    def subscribe(self, kind: str, handler: Handler) -> None:
        self._subscribers.setdefault(kind, []).append(handler)

    def subscribe_all(self, handler: Handler) -> None:
        for kind in (
            "message_created",
            "message_edited",
            "message_deleted",
            "reaction_added",
            "reaction_removed",
            "reaction_cleared",
        ):
            self.subscribe(kind, handler)

    async def publish(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("event bus is closed")
        lane_key = event_channel_id(event) or 0
        lane = self._lanes.get(lane_key)
        if lane is None:
            lane = asyncio.Queue(maxsize=self._queue_size)
            self._lanes[lane_key] = lane
            self._workers[lane_key] = asyncio.create_task(
                self._drain(lane_key, lane), name=f"bus-lane-{lane_key}"
            )
        self._inflight += 1
        self._idle.clear()
        await lane.put(event)

    async def _drain(self, lane_key: int, lane: asyncio.Queue[Event]) -> None:
        while True:
            event = await lane.get()
            try:
                await self._dispatch(event)
            finally:
                self._inflight -= 1
                if self._inflight == 0:
                    self._idle.set()
                lane.task_done()

    async def _dispatch(self, event: Event) -> None:
        handlers = self._subscribers.get(event.kind, ())
        if not handlers:
            return
        for handler in handlers:
            try:
                await handler(event)
                self.processed += 1
            except Exception:
                self.failed += 1
                log.exception(
                    "event_handler_failed",
                    kind=event.kind,
                    external_id=event_external_id(event),
                )

    async def flush(self, *, timeout: float = 30.0) -> None:  # noqa: ASYNC109
        """Wait until every published event has been dispatched."""
        async with asyncio.timeout(timeout):
            await self._idle.wait()

    async def aclose(self) -> None:
        self._closed = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.flush(), timeout=30.0)
        for worker in self._workers.values():
            worker.cancel()
        for worker in self._workers.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
        self._workers.clear()
        self._lanes.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "lanes": len(self._lanes),
            "processed": self.processed,
            "failed": self.failed,
            "inflight": self._inflight,
        }

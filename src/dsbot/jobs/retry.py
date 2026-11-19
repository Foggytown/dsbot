"""Delayed re-checks for deferred reports.

A deferred report is one waiting for follow-up photos. Two things can end the wait:

  * another message from the same author (handled by the bot, which re-examines the
    reports still waiting for that author)
  * the grace window expiring with nothing arriving (handled here)

Without the timer the second case never resolves, which is exactly what happened in
the first live test: a report sat in `deferred` forever and nobody was ever told.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from dsbot.modules.reports.service import ReportService

log = structlog.get_logger(__name__)


class RetryScheduler:
    """In-process delayed callbacks keyed by report.

    Deliberately simple: a dict of asyncio tasks. It is not durable across restarts,
    which is acceptable because a restart is exactly when a human looks at the logs.
    When the weekly scheduler lands, this can be promoted to an APScheduler job type.
    """

    def __init__(self, service: ReportService) -> None:
        self._service = service
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.fired = 0

    async def schedule(self, *, when: datetime, channel_id: int, message_id: int, key: str) -> None:
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            # A newer message pushed the deadline later; replace it.
            existing.cancel()

        delay = max(0.0, (when - datetime.now(UTC)).total_seconds())
        self._tasks[key] = asyncio.create_task(
            self._run(key, delay, channel_id, message_id), name=f"retry-{key}"
        )
        log.debug("retry_scheduled", key=key, delay_seconds=round(delay, 1))

    async def _run(self, key: str, delay: float, channel_id: int, message_id: int) -> None:
        try:
            await asyncio.sleep(delay)
            result = await self._service.retry_group(channel_id, message_id)
            self.fired += 1
            if result is not None:
                log.info(
                    "report_retried",
                    report_id=result.report_id,
                    action=result.action,
                    valid=result.valid,
                    issues=result.issues,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("report_retry_failed", channel_id=channel_id, message_id=message_id)
        finally:
            self._tasks.pop(key, None)

    async def cancel(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    def stats(self) -> dict[str, Any]:
        return {"pending": len(self._tasks), "fired": self.fired}


class ImmediateRetryScheduler:
    """Test double: records requests without waiting."""

    def __init__(self) -> None:
        self.scheduled: list[dict[str, Any]] = []

    async def schedule(self, *, when: datetime, channel_id: int, message_id: int, key: str) -> None:
        self.scheduled.append(
            {"when": when, "channel_id": channel_id, "message_id": message_id, "key": key}
        )

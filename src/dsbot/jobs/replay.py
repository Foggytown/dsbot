"""Replay stored messages through the real pipeline.

Purpose: answer "why did the bot not say anything?" without guessing. It re-processes
already-stored messages using the same service the live bot uses, optionally with a real
Discord reply sink, so every decision is observable.

    dsbot replay --channel <id>            # dry: report decisions only
    dsbot replay --channel <id> --post     # actually post/clear warnings

Also useful after a parser fix: replay the week and let warnings be recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from dsbot.modules.reports.service import ReportService
from dsbot.storage.db import Database
from dsbot.storage.models import Message, Report

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ReplayResult:
    processed: int = 0
    warned: int = 0
    valid: int = 0
    deferred: int = 0
    cleared: int = 0
    ignored: int = 0
    errors: int = 0
    lines: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "warned": self.warned,
            "valid": self.valid,
            "deferred": self.deferred,
            "cleared": self.cleared,
            "ignored": self.ignored,
            "errors": self.errors,
        }


async def replay_channel(
    db: Database,
    service: ReportService,
    *,
    channel_id: int,
    since: datetime | None = None,
    report_only: bool = True,
) -> ReplayResult:
    """Re-run every stored message head through the reports module, oldest first."""
    result = ReplayResult()

    async with db.session() as session:
        stmt = select(Message).where(
            Message.channel_id == channel_id,
            Message.deleted_at.is_(None),
            Message.author_is_bot.is_(False),
        )
        if since is not None:
            stmt = stmt.where(Message.created_at >= since)
        rows = (await session.scalars(stmt.order_by(Message.created_at))).all()

    for row in rows:
        content = (row.content or "").strip()
        attachments = len(row.attachments or [])
        # Skip messages that cannot be a report head: plain chat.
        if report_only and not content:
            continue

        try:
            outcome = await service.process_message(row.channel_id, row.author_id, row.message_id)
        except Exception:
            result.errors += 1
            log.exception("replay_message_failed", message_id=row.message_id)
            continue

        result.processed += 1
        if outcome is None:
            continue
        if not outcome.matched:
            result.ignored += 1
        elif outcome.valid:
            result.valid += 1
        elif outcome.action == "deferred":
            result.deferred += 1
        elif outcome.action == "warned":
            result.warned += 1
        if outcome.action == "cleared":
            result.cleared += 1

        result.lines.append(
            f"{row.created_at.astimezone(UTC):%H:%M:%S} msg={row.message_id} "
            f"att={attachments} matched={outcome.matched} valid={outcome.valid} "
            f"action={outcome.action} issues={outcome.issues}"
        )

    return result


async def expire_deferred(db: Database, service: ReportService) -> int:
    """Resolve deferred reports whose grace window has passed.

    Deferrals live in memory as scheduled tasks. A restart between "deferred" and the
    deadline loses them, and the report then never resolves. This runs at startup so a
    restart cannot silently swallow a warning.
    """
    fixed = 0
    async with db.session() as session:
        rows = (
            await session.scalars(
                select(Report).where(Report.status == "deferred").order_by(Report.created_at)
            )
        ).all()
        pending = [(r.channel_id, r.message_id, r.created_at) for r in rows]

    now = datetime.now(UTC)
    for channel_id, message_id, created_at in pending:
        created = (
            created_at.astimezone(UTC) if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        )
        try:
            outcome = await service.retry_group(channel_id, message_id)
        except Exception:
            log.exception("deferred_expiry_failed", message_id=message_id)
            continue
        if outcome is not None:
            fixed += 1
            log.info(
                "deferred_resolved",
                message_id=message_id,
                action=outcome.action,
                valid=outcome.valid,
                age_seconds=int((now - created).total_seconds()),
            )
    return fixed


def default_since(hours: int = 24) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)

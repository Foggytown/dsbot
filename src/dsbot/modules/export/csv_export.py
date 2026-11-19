"""CSV export of stored reports. Deliberately plain: staff can open it anywhere."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from dsbot.storage.db import Database
from dsbot.storage.models import Report, ReportField

COLUMNS: tuple[str, ...] = (
    "report_id",
    "channel_id",
    "message_id",
    "author_user_id",
    "schema_key",
    "status",
    "verdict",
    "verdict_tier_ordinal",
    "created_at",
    "attributed_user_id",
    "attribution_method",
    "fields",
    "issues",
)


async def export_reports_csv(
    db: Database,
    *,
    channel_id: int,
    since: datetime | None,
    out: Path,
) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)

    async with db.session() as session:
        stmt = select(Report).where(Report.channel_id == channel_id)
        if since is not None:
            stmt = stmt.where(Report.created_at >= since)
        reports = (await session.scalars(stmt.order_by(Report.created_at))).all()

        rows: list[dict[str, Any]] = []
        for report in reports:
            fields = (
                await session.scalars(select(ReportField).where(ReportField.report_id == report.id))
            ).all()
            rows.append(
                {
                    "report_id": report.id,
                    "channel_id": report.channel_id,
                    "message_id": report.message_id,
                    "author_user_id": report.author_user_id,
                    "schema_key": report.schema_key or "",
                    "status": report.status,
                    "verdict": report.verdict or "",
                    "verdict_tier_ordinal": report.verdict_tier_ordinal or "",
                    "created_at": report.created_at.astimezone(UTC).isoformat()
                    if report.created_at
                    else "",
                    "attributed_user_id": report.attributed_user_id or "",
                    "attribution_method": report.attribution_method,
                    "fields": " | ".join(
                        f"{f.field_id}={f.raw_value}" for f in fields if f.present
                    ),
                    "issues": " | ".join(
                        str(i.get("code")) for i in (report.issues or []) if isinstance(i, dict)
                    ),
                }
            )

    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    return len(rows)

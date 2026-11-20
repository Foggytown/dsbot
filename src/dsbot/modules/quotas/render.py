"""Turning counts into text.

Two outputs, one source of truth: a Discord message for people, and CSV for
spreadsheets. Both are produced from the same `CountSummary`, so a number in the
message and a number in the file can never disagree.

Keeping this pure (no database, no Discord) means the wording can be tested exactly,
including the awkward cases: nobody on the roster, a person with zero reports, a
conflict, and a backlog.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime

from dsbot.modules.quotas.counting import CountSummary, MemberTally

# Discord's hard cap is 2000 characters per message. Recognising a table nobody can
# read is worse than splitting it, so long rosters are paged.
MAX_MESSAGE_LENGTH = 1900
MAX_ROWS_PER_PAGE = 25


@dataclass(frozen=True, slots=True)
class RenderContext:
    """Everything the text needs that is not a number."""

    title: str
    period_label: str
    timezone: str
    generated_at: datetime | None = None
    guild_name: str = ""
    scope: str = ""


def member_line(tally: MemberTally, *, show_quota: bool = True) -> str:
    """One row. Total first, because that is what staff look for."""
    total = tally.approved_count + tally.pending_count
    parts: list[str] = []
    if show_quota and tally.required_count > 0:
        parts.append(f"{_mark(tally)} {total}/{tally.required_count}")
    else:
        parts.append(f"{_mark(tally)} {total}")

    details: list[str] = []
    if tally.pending_count:
        details.append(f"ждёт проверки {tally.pending_count}")
    if tally.rejected_count:
        details.append(f"отклонено {tally.rejected_count}")
    if tally.conflict_count:
        details.append(f"конфликт {tally.conflict_count}")
    if details:
        parts.append(f"({', '.join(details)})")

    who = tally.display_name or f"<@{tally.user_id}>"
    if tally.tier_label:
        who = f"{who} — {tally.tier_label}"
    return f"{'  '.join(parts)} — {who}"


def _mark(tally: MemberTally) -> str:
    """A quota marker. Deliberately not a colour: the text must be readable as a log."""
    if tally.required_count <= 0:
        return "•"
    return "✅" if tally.met else "⚠️"


def render_message(summary: CountSummary, context: RenderContext) -> list[str]:
    """Render pages of a Discord message, ready to post in order.

    An empty roster is called out explicitly. A silent empty table looks like success,
    and the actual cause (no ladder, or the Members intent off) would stay invisible.
    """
    header: list[str] = [f"**{context.title}**"]
    header.append(f"Период: {context.period_label} ({context.timezone})")
    if context.scope:
        header.append(context.scope)

    rows = summary.everyone()
    total = summary.credited_total()
    header.append(
        f"Рапортов учтено: **{total}**"
        + (f", ждут проверки: {summary.pending_total()}" if summary.pending_total() else "")
    )

    if not rows:
        header.append("")
        header.append("Никого с настроенной ролью не найдено — считать не для кого.")
        return ["\n".join(header)]

    blocked = [r for r in rows if r.conflict_count]
    if blocked:
        header.append(
            f"⚠️ Рапортов с конфликтом вердиктов: **{sum(r.conflict_count for r in blocked)}** "
            "— они не зачтены."
        )

    pages: list[str] = []
    current = list(header)
    count = 0
    for row in rows:
        line = member_line(row)
        too_long = len("\n".join([*current, line])) > MAX_MESSAGE_LENGTH
        if count and (count >= MAX_ROWS_PER_PAGE or too_long):
            pages.append("\n".join(current))
            current = list(header[:1])
            count = 0
        current.append(line)
        count += 1
    pages.append("\n".join(current))

    unranked = [r for r in summary.unranked if r.approved_count or r.pending_count]
    if unranked:
        pages[-1] += (
            "\n\nНе в списке ролей, но рапорты есть: "
            + ", ".join(r.display_name or f"<@{r.user_id}>" for r in unranked[:10])
            + (" и др." if len(unranked) > 10 else "")
        )
        pages[-1] += "\nПроверьте, что у этих людей выдана нужная роль."
    return pages


CSV_COLUMNS: tuple[str, ...] = (
    "user_id",
    "display_name",
    "tier",
    "tier_ordinal",
    "required",
    "approved",
    "pending",
    "rejected",
    "conflict",
    "met",
)


def render_csv(summary: CountSummary) -> str:
    """A CSV with a BOM, so Excel opens Cyrillic names correctly on Windows."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for row in summary.everyone():
        writer.writerow(
            {
                "user_id": row.user_id,
                "display_name": row.display_name,
                "tier": row.tier_label or "",
                "tier_ordinal": row.tier_ordinal if row.tier_ordinal is not None else "",
                "required": row.required_count,
                "approved": row.approved_count,
                "pending": row.pending_count,
                "rejected": row.rejected_count,
                "conflict": row.conflict_count,
                "met": "yes" if row.met else "no",
            }
        )
    # csv writes "\r\n" per row; that is correct for the format and for Excel, so the
    # BOM goes on the front and the body is left exactly as the module produced it.
    return "\ufeff" + buffer.getvalue()


__all__ = [
    "CSV_COLUMNS",
    "MAX_MESSAGE_LENGTH",
    "MAX_ROWS_PER_PAGE",
    "RenderContext",
    "member_line",
    "render_csv",
    "render_message",
]

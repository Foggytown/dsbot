"""Turning counts into text.

Two outputs, one source of truth: a Discord message for people, and CSV for
spreadsheets. Both are produced from the same `CountSummary`, so a number in the
message and a number in the file can never disagree.

Keeping this pure (no database, no Discord) means the wording can be tested exactly,
including the awkward cases: nobody on the roster, a person with zero reports, a
conflict, and a backlog.

Rows are grouped by tier, highest first, because that is how the people reading it think:
"the instructors", "the seniors". A tier can be hidden from the message with `hidden_tiers`
for channels where one group is noise.
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

# What an unknown quota tier is called in the grouping.
NO_TIER = "Без тира"


@dataclass(frozen=True, slots=True)
class RenderContext:
    """Everything the text needs that is not a number."""

    title: str
    period_label: str
    timezone: str
    generated_at: datetime | None = None
    guild_name: str = ""
    scope: str = ""
    # Tier labels or keys excluded from the message. Case-insensitive.
    hidden_tiers: tuple[str, ...] = ()


def member_line(tally: MemberTally, *, show_quota: bool = True) -> str:
    """One row. Total first, then a ping, because staff need to notify people.

    The ping is the way to reach someone from the report, so the name is always
    `<@id>` rather than a stale nickname with no link back to the account.

    With a quota, the total is clamped to the requirement: "3/2" invites the question
    "out of what?", and overfilling a quota is not extra credit.
    """
    total = tally.credited_total
    parts: list[str] = []
    if show_quota and tally.required_count > 0:
        shown = min(total, tally.required_count)
        parts.append(f"{_mark(tally)} {shown}/{tally.required_count}")
        if total > tally.required_count:
            parts.append(f"+{total - tally.required_count}")
    else:
        parts.append(f"{_mark(tally)} {total}")

    details: list[str] = []
    if tally.pending_count:
        details.append(f"ждёт проверки {tally.pending_count}")
    if tally.unknown_approval_count:
        details.append(f"одобрено без прав {tally.unknown_approval_count}")
    if tally.rejected_count:
        details.append(f"отклонено {tally.rejected_count}")
    if tally.conflict_count:
        details.append(f"конфликт {tally.conflict_count}")
    if details:
        parts.append(f"({', '.join(details)})")

    who = f"<@{tally.user_id}>"
    if tally.display_name:
        who = f"{who} {tally.display_name}"
    return f"{'  '.join(parts)} — {who}"


def _mark(tally: MemberTally) -> str:
    """A quota marker. Deliberately not a colour: the text must be readable as a log."""
    if tally.required_count <= 0:
        return "•"
    return "✅" if tally.met else "⚠️"


def _tier_key(tally: MemberTally) -> str:
    return tally.tier_label or NO_TIER


def group_by_tier(rows: list[MemberTally]) -> list[tuple[str, list[MemberTally]]]:
    """Group rows by tier, highest tier first, then by volume within each tier.

    Ordering by the tier's ordinal (not its name) means a report reads top-down like the
    hierarchy, and the same tiers keep the same order between runs.
    """
    buckets: dict[str, list[MemberTally]] = {}
    ordinals: dict[str, int] = {}
    for row in rows:
        key = _tier_key(row)
        buckets.setdefault(key, []).append(row)
        ordinal = row.tier_ordinal if row.tier_ordinal is not None else -1
        ordinals[key] = max(ordinals.get(key, -1), ordinal)

    ordered = sorted(buckets.items(), key=lambda item: -ordinals[item[0]])
    for _key, members in ordered:
        members.sort(key=_row_sort_key)
    return ordered


def _row_sort_key(tally: MemberTally) -> tuple[int, int, str]:
    """Most credits first, then those with a quota, then by name for stability."""
    return (
        -tally.credited_total,
        -(tally.required_count > 0),
        tally.display_name or str(tally.user_id),
    )


def render_message(summary: CountSummary, context: RenderContext) -> list[str]:
    """Render pages of a Discord message, ready to post in order.

    An empty roster is called out explicitly. A silent empty table looks like success,
    and the actual cause (no ladder, or the Members intent off) would stay invisible.
    """
    header: list[str] = [f"**{context.title}**"]
    header.append(f"Период: {context.period_label} ({context.timezone})")
    if context.scope:
        header.append(context.scope)

    hidden = {t.strip().lower() for t in context.hidden_tiers if t.strip()}
    rows = [row for row in summary.everyone() if _tier_key(row).strip().lower() not in hidden]
    total = sum(row.credited_total for row in rows)
    pending = sum(row.pending_count for row in rows)
    header.append(
        f"Рапортов учтено: **{total}**" + (f", из них ждут проверки: {pending}" if pending else "")
    )

    if not rows:
        header.append("")
        if hidden and summary.everyone():
            header.append("Все тиры скрыты в настройках этого канала.")
        else:
            header.append("Никого с настроенной ролью не найдено — считать не для кого.")
        return ["\n".join(header)]

    blocked = [r for r in rows if r.conflict_count]
    if blocked:
        header.append(
            f"⚠️ Рапортов с конфликтом вердиктов: **{sum(r.conflict_count for r in blocked)}** "
            "— они не зачтены."
        )
    unknown = sum(r.unknown_approval_count for r in rows)
    if unknown:
        header.append(
            f"ℹ️ Рапортов, одобренных без подтверждённых прав: **{unknown}** "
            "— зачтены, но проверьте, кто это сделал."
        )

    pages: list[str] = []
    current = list(header)
    row_count = 0

    for tier, members in group_by_tier(rows):
        heading = f"\n__{tier}__"
        # A tier heading must not be left dangling at the bottom of a page.
        if row_count and (
            row_count >= MAX_ROWS_PER_PAGE
            or len("\n".join([*current, heading])) > MAX_MESSAGE_LENGTH
        ):
            pages.append("\n".join(current))
            current = list(header[:1])
            row_count = 0
        current.append(heading)
        row_count += 1

        for row in members:
            line = member_line(row)
            too_long = len("\n".join([*current, line])) > MAX_MESSAGE_LENGTH
            if row_count and (row_count >= MAX_ROWS_PER_PAGE or too_long):
                pages.append("\n".join(current))
                current = list(header[:1])
                row_count = 0
            current.append(line)
            row_count += 1

    pages.append("\n".join(current))

    unranked = [
        r
        for r in summary.unranked
        if (r.approved_count or r.pending_count or r.unknown_approval_count)
        and _tier_key(r).strip().lower() not in hidden
    ]
    if unranked:
        pages[-1] += (
            "\n\nНе в списке ролей, но рапорты есть: "
            + ", ".join(f"<@{r.user_id}>" for r in unranked[:10])
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
    "approved_unknown",
    "rejected",
    "conflict",
    "met",
)


def _met_cell(tally: MemberTally) -> str:
    """Three states, not two: no quota is not the same as a missed quota."""
    if tally.required_count <= 0:
        return "no-quota"
    return "yes" if tally.met else "no"


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
                "approved_unknown": row.unknown_approval_count,
                "rejected": row.rejected_count,
                "conflict": row.conflict_count,
                "met": _met_cell(row),
            }
        )
    # csv writes "\r\n" per row; that is correct for the format and for Excel, so the
    # BOM goes on the front and the body is left exactly as the module produced it.
    return "\ufeff" + buffer.getvalue()


__all__ = [
    "CSV_COLUMNS",
    "MAX_MESSAGE_LENGTH",
    "MAX_ROWS_PER_PAGE",
    "NO_TIER",
    "RenderContext",
    "group_by_tier",
    "member_line",
    "render_csv",
    "render_message",
]

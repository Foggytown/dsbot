"""Shows the exact message staff would receive, for review."""

from __future__ import annotations

from dsbot.modules.quotas.counting import CountSummary, MemberTally
from dsbot.modules.quotas.render import RenderContext, render_message

ctx = RenderContext(
    title="Отчёт по рапортам",
    period_label="09.11.2026–15.11.2026 (7 дн.)",
    timezone="Europe/Moscow",
    scope="Канал: #reports · история Discord",
)

summary = CountSummary(
    tallies=[
        MemberTally(
            user_id=101,
            display_name="Guard | Liam",
            tier_label="Старший инс +",
            tier_ordinal=50,
            approved_count=3,
            required_count=3,
        ),
        MemberTally(
            user_id=102,
            display_name="CT | Raven-21",
            tier_label="Старший инс +",
            tier_ordinal=50,
            approved_count=1,
            pending_count=1,
            required_count=3,
        ),
        MemberTally(
            user_id=201,
            display_name="Ghost | Mar",
            tier_label="Instructor",
            tier_ordinal=10,
            approved_count=2,
            unknown_approval_count=1,
            required_count=2,
        ),
        MemberTally(
            user_id=202,
            display_name="SOB | Alpha-12",
            tier_label="Instructor",
            tier_ordinal=10,
            rejected_count=1,
            required_count=2,
        ),
        MemberTally(
            user_id=301,
            display_name="SOB | Alpha-17",
            tier_label="Исключение",
            tier_ordinal=30,
            required_count=0,
        ),
    ],
    outcomes=[],
)

print("=== everything visible ===")
for page in render_message(summary, ctx):
    print(page)

print()
print("=== with 'Исключение' hidden, as an admin can set ===")
hidden = RenderContext(
    title=ctx.title,
    period_label=ctx.period_label,
    timezone=ctx.timezone,
    scope=ctx.scope,
    hidden_tiers=("Исключение",),
)
for page in render_message(summary, hidden):
    print(page)

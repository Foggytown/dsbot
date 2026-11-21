"""Shows the exact message staff would receive, for review.

    uv run python scripts/preview-report.py

No personal details are hardcoded: the developer ping is read from settings, so this
script shows exactly what the deployment is configured to show.
"""

from __future__ import annotations

from dsbot.config import get_settings
from dsbot.modules.quotas.counting import CountSummary, MemberTally
from dsbot.modules.quotas.render import RenderContext, render_message

settings = get_settings()

ctx = RenderContext(
    period_label="09.11.2026–15.11.2026 (7 дн.)",
    timezone="Europe/Moscow",
    scope="Канал: #reports · история Discord",
    developer_user_id=settings.developer_user_id,
)

summary = CountSummary(
    tallies=[
        MemberTally(
            user_id=101,
            display_name="Guard | Liam",
            tier_label="Старший инс +",
            tier_ordinal=50,
            approved_count=4,
            required_count=1,
        ),
        MemberTally(
            user_id=102,
            display_name="CT | Raven-21",
            tier_label="Старший инс +",
            tier_ordinal=50,
            approved_count=1,
            pending_count=1,
            required_count=1,
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
)

print("=== everything visible ===")
for page in render_message(summary, ctx):
    print(page)

print()
print("=== only the Instructor tier, as /reports count tier:Instructor ===")
only = RenderContext(
    period_label=ctx.period_label,
    timezone=ctx.timezone,
    scope=ctx.scope,
    only_tier="Instructor",
)
for page in render_message(summary, only):
    print(page)

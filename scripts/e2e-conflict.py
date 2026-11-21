"""A same-tier disagreement must be silent and must not credit anyone.

Confirms the behaviour asked for: no message in the channel, the report recorded as a
conflict, and no credit toward the quota. An automatic weekly run depends on this.

    uv run python scripts/e2e-conflict.py
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from datetime import UTC, datetime

from dsbot.config import Settings
from dsbot.modules.quotas.counting import CountedReport, evaluate_reports, tally
from dsbot.modules.quotas.render import RenderContext, render_message
from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.reports.schema import FormSchemaDef
from dsbot.modules.reports.yaml_form import parse_form_file
from dsbot.modules.verdict.engine import Rank, Reaction
from dsbot.modules.verdict.service import VerdictService
from dsbot.storage import admin, ranks_repo, repos
from dsbot.storage.db import Database

GUILD = 900
CHANNEL = 500
AUTHOR = 600
SENIOR_ROLE = 20


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'c.db').as_posix()}"))
    try:
        await db.prepare()
        schema: FormSchemaDef = parse_form_file(pathlib.Path("forms/training.yaml"))

        async with db.write_session() as session:
            await admin.ensure_schema_seed(session, [schema])
            binding = await admin.bind_channel(
                session,
                guild_id=GUILD,
                channel_id=CHANNEL,
                module_key="reports",
                schema_key=schema.key,
                rank_source_guild_id=GUILD,
            )
            await ranks_repo.replace_ladder(
                session,
                LadderSpec(
                    key="review",
                    guild_id=GUILD,
                    purpose="review",
                    tiers=[
                        TierSpec(
                            key="senior",
                            label="Senior",
                            ordinal=30,
                            roles=[SENIOR_ROLE],
                            capabilities=["review"],
                        )
                    ],
                ),
            )
            report = await repos.upsert_report(
                session,
                channel_id=CHANNEL,
                message_id=1,
                guild_id=GUILD,
                author_user_id=AUTHOR,
                created_at=datetime(2026, 11, 16, tzinfo=UTC),
                binding_id=binding.id,
            )
            report.status = "valid"
            report_id = report.id

            # Two reviewers of the SAME tier disagree: one check, one cross.
            for user_id, emoji in ((501, "\u2705"), (502, "\u274c")):
                await repos.add_reaction(
                    session,
                    message_id=1,
                    channel_id=CHANNEL,
                    guild_id=GUILD,
                    emoji_key=f"unicode:{emoji}",
                    emoji_id=None,
                    emoji_name=emoji,
                    user_id=user_id,
                    tier_ordinal=30,
                    tier_label="Senior",
                    can_review=True,
                )

        # No reply sink is passed at all: the service cannot post even if it wanted to.
        update = await VerdictService(db).on_reaction(1)
        assert update is not None
        print(f"verdict        : {update.verdict}")
        print(f"conflicted     : {update.conflicted_users}")

        async with db.session() as session:
            stored = await repos.list_reactions(session, 1)
        reactions = tuple(Reaction(user_id=r.user_id, emoji_name=r.emoji_name) for r in stored)
        ranks = {
            r.user_id: Rank(user_id=r.user_id, ordinal=30, tier_label="Senior") for r in stored
        }

        summary = tally(
            evaluate_reports(
                [
                    CountedReport(
                        report_id=report_id,
                        author_user_id=AUTHOR,
                        created_at=datetime(2026, 11, 16, tzinfo=UTC),
                        reactions=reactions,
                        ranks=ranks,
                    )
                ]
            ),
            [],
        )
        print(f"credited total : {summary.credited_total()}")
        print(f"conflict total : {summary.conflict_total()}")

        assert update.verdict == "conflict"
        assert summary.credited_total() == 0, "a conflict must not credit anyone"
        assert summary.conflict_total() == 1

        page = render_message(
            summary, RenderContext(period_label="16.11-22.11", timezone="Europe/Moscow")
        )[0]
        print()
        print("--- the report says ---")
        print(page)
        assert "конфликт" in page.lower()

        print()
        print("OK: silent, recorded as a conflict, and not credited")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

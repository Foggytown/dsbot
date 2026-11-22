"""Proves `the scheduled close` publishes the same numbers as `/reports count`.

The bug: `publish_period` accepted the Discord count and then re-counted from the
database, so every report rendered as unreviewed - the archive only holds reactions the
bot happened to see live. Both paths must now be identical.

This drives a fake channel with real reactions through both entry points and compares
what each one renders.

    uv run python scripts/e2e-run-matches-count.py
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

from dsbot.config import Settings
from dsbot.discord.reply import RecordingReplySink
from dsbot.modules.quotas import close as close_module
from dsbot.modules.quotas.period import current_week
from dsbot.modules.quotas.schedule import parse_close
from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.reports.schema import DEFAULT_SCHEMAS
from dsbot.storage import admin, ranks_repo, repos
from dsbot.storage.db import Database

GUILD = 1274402776696488108
CHANNEL = 1274460260941434901
DESTINATION = 1573045809774067812
AUTHOR = 5001
REVIEWER = 6001
REVIEW_ROLE = 9001
QUOTA_ROLE = 9002
MOSCOW = "Europe/Moscow"

VALID_REPORT = """1. SOB | 0016
2. INF
3. Доказательства:
     - 3.1. фото
     - 3.2. фото
4. https://discord.com/channels/1274402776696488108/1339645935348613191/1348671185625415771"""


class _Emoji:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = None


class _Reaction:
    def __init__(self, name: str, users: list[Any]) -> None:
        self.emoji = _Emoji(name)
        self.count = len(users)
        self._users = users

    def users(self, limit: int = 100) -> Any:
        async def gen() -> Any:
            for user in self._users:
                yield user

        return gen()


class _User:
    def __init__(self, user_id: int, bot: bool = False) -> None:
        self.id = user_id
        self.bot = bot


class _Author(_User):
    def __init__(self, user_id: int, name: str) -> None:
        super().__init__(user_id)
        self.name = name
        self.display_name = name


class _Message:
    def __init__(
        self,
        message_id: int,
        content: str,
        created: datetime,
        attachments: int = 0,
        reactions: list[_Reaction] | None = None,
    ) -> None:
        self.id = message_id
        self.content = content
        self.created_at = created
        self.edited_at = None
        self.attachments = [object()] * attachments
        self.reactions = reactions or []
        self.author = _Author(AUTHOR, "Instructor")


class _Channel:
    def __init__(self, channel_id: int, messages: list[_Message]) -> None:
        self.id = channel_id
        self._messages = messages
        self.sent: list[str] = []

    def history(self, **kwargs: Any) -> Any:
        after = kwargs.get("after")
        before = kwargs.get("before")

        async def gen() -> Any:
            for message in sorted(self._messages, key=lambda m: m.created_at, reverse=True):
                if after and message.created_at <= after:
                    continue
                if before and message.created_at >= before:
                    continue
                yield message

        return gen()

    async def send(self, content: str, **kwargs: Any) -> Any:
        self.sent.append(content)
        return _Probe()

    async def fetch_message(self, message_id: int) -> Any:
        return next(m for m in self._messages if m.id == message_id)


class _Probe:
    async def delete(self) -> None:
        return None


class _Role:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class _Member:
    def __init__(self, user_id: int, roles: list[int]) -> None:
        self.id = user_id
        self.roles = [_Role(r) for r in roles]


class _Guild:
    """Just enough guild for DiscordRankSource: get_member plus fetch_member."""

    id = GUILD

    def get_member(self, user_id: int) -> _Member | None:
        if user_id == REVIEWER:
            return _Member(user_id, [REVIEW_ROLE])
        return None

    async def fetch_member(self, user_id: int) -> _Member | None:
        return self.get_member(user_id)


class _Client:
    def __init__(self, channel: _Channel) -> None:
        self._channel = channel
        self._destination = _Channel(DESTINATION, [])

    def get_channel(self, channel_id: int) -> Any:
        if channel_id == DESTINATION:
            return self._destination
        return self._channel if channel_id == self._channel.id else None

    async def fetch_channel(self, channel_id: int) -> Any:
        return self.get_channel(channel_id)

    def get_guild(self, guild_id: int) -> Any:
        return _Guild() if guild_id == GUILD else None


class _RankSource:
    """The reviewer holds the review role; everyone else holds nothing."""

    async def role_ids_for(self, guild_id: int, user_id: int) -> frozenset[int]:
        return frozenset({REVIEW_ROLE}) if user_id == REVIEWER else frozenset()


def tidy(text: str) -> str:
    """Compare the substance, not the whitespace or the scope line."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return re.sub(r"\s+", " ", " ".join(lines))


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp / 'run.db'}"))
    await db.create_all()
    await db.prepare()

    try:
        period = current_week(datetime(2026, 11, 22, 15, tzinfo=UTC), MOSCOW)
        # Inside the window, not on its edge: the period is half-open, so a message at
        # exactly `start` belongs to the previous one.
        created = period.start + timedelta(hours=1)

        async with db.write_session() as session:
            await admin.ensure_schema_seed(session, DEFAULT_SCHEMAS)
            binding = await admin.bind_channel(
                session,
                guild_id=GUILD,
                channel_id=CHANNEL,
                module_key="reports",
                schema_key="training_report",
                locale="ru",
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
                            roles=[REVIEW_ROLE],
                            capabilities=["review"],
                        )
                    ],
                ),
            )
            await ranks_repo.replace_ladder(
                session,
                LadderSpec(
                    key="quota",
                    guild_id=GUILD,
                    purpose="quota",
                    tiers=[
                        TierSpec(
                            key="instructor",
                            label="Instructor",
                            ordinal=10,
                            roles=[QUOTA_ROLE],
                            required_per_week=1,
                        )
                    ],
                ),
            )
            await repos.replace_roster(
                session,
                (await ranks_repo.load_all_ladders(session))[GUILD].quota_ladder_id or 0,
                [
                    {
                        "user_id": AUTHOR,
                        "display_name": "Instructor",
                        "tier_label": "Instructor",
                        "tier_ordinal": 10,
                        "required_per_week": 1,
                    }
                ],
            )
            row = await repos.get_binding_by_id(session, binding.id)
            assert row is not None
            config = dict(row.config or {})
            config["report_channel_id"] = DESTINATION
            row.config = config

        # A report WITH an approval, present only in Discord history.
        messages = [
            _Message(
                1001,
                VALID_REPORT,
                created,
                attachments=2,
                reactions=[_Reaction("\u2705", [_User(REVIEWER)])],
            )
        ]
        channel = _Channel(CHANNEL, messages)
        client = _Client(channel)

        from dsbot.modules.ranks.resolver import RankResolver

        resolver = RankResolver(_RankSource(), {})
        async with db.session() as session:
            resolver.set_ladders(await ranks_repo.load_all_ladders(session))

        # --- what /reports count renders -------------------------------
        from dsbot.modules.quotas.live import count_channel_from_discord
        from dsbot.modules.quotas.render import RenderContext, render_message

        counted = await count_channel_from_discord(
            channel=channel,
            db=db,
            channel_id=CHANNEL,
            period=period,
            resolver=resolver,
        )
        count_pages = render_message(
            counted.summary,
            RenderContext(period_label=period.describe(MOSCOW), timezone=MOSCOW),
        )

        # --- what the scheduled close publishes -------------------------------
        close_at = parse_close("22.11.2026 23:59:59", MOSCOW)
        async with db.write_session() as session:
            await repos.set_binding_schedule(
                session, binding_id=binding.id, next_close=close_at, period_days=7
            )
        sink = RecordingReplySink()
        outcome = await close_module.close_period(
            db=db,
            client=client,
            binding_id=binding.id,
            now=close_at,
            reply=sink,
        )
        assert outcome.action == "published", outcome.problem

        print("WHAT /reports count SHOWS")
        print("=" * 78)
        for page in count_pages:
            print(page)
        print()
        print("WHAT the scheduled close PUBLISHED")
        print("=" * 78)
        for _, body in sink.sent:
            print(body)

        published = sink.sent[0][1] if sink.sent else ""
        print()

        # The stored audit trail must say the same thing as the channel. This is the part
        # that was wrong before: the table showed real verdicts while weekly_results held
        # a recount from the archive, where every report looked unreviewed.
        async with db.session() as session:
            run = await repos.get_run_by_period(
                session,
                binding_id=binding.id,
                period_start=period.start,
                period_end=period.end,
            )
            assert run is not None, "the run must be stored"
            stored = await repos.list_run_results(session, run.id)

        print("WHAT WAS STORED IN weekly_results")
        print("=" * 78)
        for row in stored:
            print(
                f"  user {row.user_id} approved={row.approved_count} "
                f"pending={row.pending_count} unknown={row.unknown_approval_count} "
                f"required={row.required_count} met={row.met}"
            )
        print()

        assert stored, "the run must have per-person rows"
        row = stored[0]
        assert row.approved_count == 1, (
            "the stored row must show the approval the channel shows - a database recount "
            "would store 0 and mark it pending"
        )
        assert row.pending_count == 0
        assert row.met is True, "the stored row must agree that the quota was met"

        assert "✅" in published, (
            "the approval is visible in Discord history, so the published report must "
            "show it - a database recount would render it as unreviewed"
        )
        assert "1/1" in published, "the approved report must be credited"

        # Compare the substance, not the scope line. The manual count has no scope here and
        # the published one names its channel, which is a deliberate difference.
        def substance(text: str) -> str:
            return " ".join(
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.startswith("Канал:")
            )

        assert substance(published) == substance(count_pages[0]), (
            "the scheduled report must say the same thing as the manual count"
        )
        print()
        print("OK: the scheduled close publishes the Discord count, identical to /reports count")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

"""The whole wiring must be rebuildable from git on every deploy.

On the hosting panel only `/data` survives: the database lives there, but the checkout is
re-uploaded each time. So a channel's form, its review ladder and its quota ladder must
all come from files, and startup must reconcile them every single time — not only when
the database happens to be empty.

These tests pin exactly that, because getting it wrong looks like "the YAML change did
nothing" after a deploy, which is indistinguishable from a broken bot.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dsbot.config import Settings
from dsbot.modules.reports.channels import ChannelsFile, ChannelSpec, load_channels_file
from dsbot.modules.reports.channels_sync import sync_channels
from dsbot.storage import repos
from dsbot.storage.db import Database

GUILD = 900
CHANNEL = 500


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(
        Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")  # type: ignore[call-arg]
    )
    await database.prepare()
    yield database
    await database.dispose()


def write_ladder(directory: Path, key: str, *, purpose: str, guild: int = GUILD) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if purpose == "quota":
        tiers = (
            "  - key: instructor\n    label: Instructor\n    ordinal: 10\n"
            "    roles: [555]\n    required_per_week: 3\n"
        )
    else:
        tiers = (
            "  - key: senior\n    label: Senior Instructor\n    ordinal: 30\n"
            "    roles: [777]\n    capabilities: [review, admin]\n"
        )
    (directory / f"{key}.yaml").write_text(
        f"key: {key}\npurpose: {purpose}\nguild_id: {guild}\ntiers:\n{tiers}",
        encoding="utf-8",
    )


def channels(*entries: ChannelSpec) -> ChannelsFile:
    return ChannelsFile(channels=list(entries))


async def test_sync_applies_the_ladder_a_channel_names(db: Database, tmp_path: Path) -> None:
    ladders = tmp_path / "ladders"
    write_ladder(ladders, "review", purpose="review")
    write_ladder(ladders, "quota", purpose="quota")

    async with db.write_session() as session:
        result = await sync_channels(
            session,
            channels(
                ChannelSpec(
                    guild=GUILD,
                    channel=CHANNEL,
                    review_ladder="review",
                    quota_ladder="quota",
                )
            ),
            ladders_dir=ladders,
        )

    assert sorted(result.ladders) == sorted([f"{GUILD}/quota", f"{GUILD}/review"])

    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
    assert binding is not None
    assert binding.rank_ladder_id is not None, "the review ladder must be attached"
    assert (binding.config or {}).get("quota_ladder") == "quota"


async def test_a_second_run_changes_nothing(db: Database, tmp_path: Path) -> None:
    ladders = tmp_path / "ladders"
    write_ladder(ladders, "review", purpose="review")
    entry = channels(ChannelSpec(guild=GUILD, channel=CHANNEL, review_ladder="review"))

    async with db.write_session() as session:
        await sync_channels(session, entry, ladders_dir=ladders)
    async with db.write_session() as session:
        second = await sync_channels(session, entry, ladders_dir=ladders)

    assert second.created == []
    assert second.updated == []


async def test_a_redeploy_keeps_the_scraped_roster(db: Database, tmp_path: Path) -> None:
    """The panel re-uploads everything but /data and syncs on every start.

    If an unchanged ladder were rebuilt each time, the roster (which references the
    ladder) would be wiped on every deploy, and the weekly report would claim nobody is
    tracked. Identical ladders must therefore be kept, not replaced.
    """
    from dsbot.storage import ranks_repo

    ladders = tmp_path / "ladders"
    write_ladder(ladders, "quota", purpose="quota")
    entry = channels(ChannelSpec(guild=GUILD, channel=CHANNEL, quota_ladder="quota"))

    async with db.write_session() as session:
        await sync_channels(session, entry, ladders_dir=ladders)
    async with db.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
        ladder_id = group.quota_ladder_id or 0
    async with db.write_session() as session:
        await repos.replace_roster(
            session,
            ladder_id,
            [{"user_id": 1, "display_name": "Ann", "tier_ordinal": 10, "required_per_week": 3}],
        )

    # A redeploy: same files, sync again.
    async with db.write_session() as session:
        await sync_channels(session, entry, ladders_dir=ladders)

    async with db.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
        roster = await repos.list_roster(session, group.quota_ladder_id or 0)
    assert [row.user_id for row in roster] == [1], "a redeploy must not lose the roster"


async def test_changing_a_ladder_rebuilds_it(db: Database, tmp_path: Path) -> None:
    """A real change must still take effect, roster rebuild is allowed then."""
    from dsbot.storage import ranks_repo

    ladders = tmp_path / "ladders"
    write_ladder(ladders, "quota", purpose="quota")
    entry = channels(ChannelSpec(guild=GUILD, channel=CHANNEL, quota_ladder="quota"))
    async with db.write_session() as session:
        await sync_channels(session, entry, ladders_dir=ladders)

    # The admin raises the requirement from 3 to 5.
    (ladders / "quota.yaml").write_text(
        f"key: quota\npurpose: quota\nguild_id: {GUILD}\ntiers:\n"
        "  - key: instructor\n    label: Instructor\n    ordinal: 10\n"
        "    roles: [555]\n    required_per_week: 5\n",
        encoding="utf-8",
    )
    async with db.write_session() as session:
        await sync_channels(session, entry, ladders_dir=ladders)

    async with db.session() as session:
        group = (await ranks_repo.load_all_ladders(session))[GUILD]
        index = group.indexes[0]
    assert index.quota_by_role[555] == 5


async def test_a_named_ladder_that_does_not_exist_is_an_error(db: Database, tmp_path: Path) -> None:
    """Silently counting nobody is worse than refusing to start."""
    with pytest.raises(ValueError) as excinfo:
        async with db.write_session() as session:
            await sync_channels(
                session,
                channels(ChannelSpec(guild=GUILD, channel=CHANNEL, review_ladder="missing")),
                ladders_dir=tmp_path / "empty",
            )
    assert "missing" in str(excinfo.value)


async def test_a_ladder_for_another_guild_is_refused(db: Database, tmp_path: Path) -> None:
    """Roles must come from the server the channel points at, or approvals resolve to nobody."""
    ladders = tmp_path / "ladders"
    write_ladder(ladders, "review", purpose="review", guild=999)

    with pytest.raises(ValueError) as excinfo:
        async with db.write_session() as session:
            await sync_channels(
                session,
                channels(ChannelSpec(guild=GUILD, channel=CHANNEL, review_ladder="review")),
                ladders_dir=ladders,
            )
    assert "guild" in str(excinfo.value)


async def test_a_ladder_with_the_wrong_key_is_refused(db: Database, tmp_path: Path) -> None:
    ladders = tmp_path / "ladders"
    write_ladder(ladders, "review", purpose="review")
    # Channel asks for "team", but the file inside says key: review.
    (ladders / "team.yaml").write_text(
        (ladders / "review.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(ValueError) as excinfo:
        async with db.write_session() as session:
            await sync_channels(
                session,
                channels(ChannelSpec(guild=GUILD, channel=CHANNEL, review_ladder="team")),
                ladders_dir=ladders,
            )
    assert "declares key" in str(excinfo.value)


async def test_a_ladder_blocking_problem_stops_the_sync(db: Database, tmp_path: Path) -> None:
    """A quota ladder that grants review would corrupt every verdict, so refuse it."""
    ladders = tmp_path / "ladders"
    ladders.mkdir(parents=True)
    (ladders / "quota.yaml").write_text(
        f"key: quota\npurpose: quota\nguild_id: {GUILD}\ntiers:\n"
        "  - key: instructor\n    label: Instructor\n    ordinal: 10\n"
        "    roles: [5]\n    required_per_week: 3\n    capabilities: [review]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        async with db.write_session() as session:
            await sync_channels(
                session,
                channels(ChannelSpec(guild=GUILD, channel=CHANNEL, quota_ladder="quota")),
                ladders_dir=ladders,
            )


async def test_a_channel_may_name_no_ladders(db: Database, tmp_path: Path) -> None:
    """Parsing and warnings work before any hierarchy exists."""
    async with db.write_session() as session:
        result = await sync_channels(
            session,
            channels(ChannelSpec(guild=GUILD, channel=CHANNEL)),
            ladders_dir=tmp_path / "empty",
        )
    assert result.created == [CHANNEL]
    async with db.session() as session:
        binding = await repos.get_binding_by_channel(session, CHANNEL)
    assert binding is not None
    assert binding.rank_ladder_id is None


def test_shipped_channels_file_parses() -> None:
    """The real file must stay valid; it is applied on every deployment."""
    spec = load_channels_file(Path("forms/channels.yaml"))
    assert spec.channels, "the shipped file must list at least one channel"
    for entry in spec.channels:
        assert entry.guild > 0 and entry.channel > 0


def test_shipped_ladder_templates_are_usable() -> None:
    """The templates are copied by hand, so they must parse as-is."""
    from dsbot.modules.ranks.ladder import load_ladder

    review = load_ladder(Path("forms/ladders/review.yaml.example"))
    quota = load_ladder(Path("forms/ladders/quota.yaml.example"))
    assert review.purpose == "review"
    assert quota.purpose == "quota"
    assert any(t.required_per_week is not None for t in quota.tiers)

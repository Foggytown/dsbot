"""Simulates a panel redeploy: the checkout is replaced, only /data survives.

Proves that channels, forms and ladders all come back from git, and that a scraped
roster survives because an unchanged ladder is not rebuilt.

    uv run python scripts/e2e-deploy.py
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from dsbot.config import Settings
from dsbot.modules.reports.channels import ChannelsFile, ChannelSpec
from dsbot.modules.reports.channels_sync import sync_channels
from dsbot.storage import ranks_repo, repos
from dsbot.storage.db import Database

GUILD = 900
CHANNEL = 500


def write_ladders(directory: Path, requirement: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "review.yaml").write_text(
        f"key: review\npurpose: review\nguild_id: {GUILD}\ntiers:\n"
        "  - key: senior\n    label: Senior Instructor\n    ordinal: 30\n"
        "    roles: [777]\n    capabilities: [review, admin]\n",
        encoding="utf-8",
    )
    (directory / "quota.yaml").write_text(
        f"key: quota\npurpose: quota\nguild_id: {GUILD}\ntiers:\n"
        "  - key: instructor\n    label: Instructor\n    ordinal: 10\n"
        f"    roles: [555]\n    required_per_week: {requirement}\n",
        encoding="utf-8",
    )


async def main() -> None:
    data_dir = Path(tempfile.mkdtemp())  # stands in for /data: survives everything
    checkout = Path(tempfile.mkdtemp())  # stands in for the uploaded code
    settings = Settings(database_url=f"sqlite+aiosqlite:///{(data_dir / 'dsbot.db').as_posix()}")
    db = Database(settings)
    try:
        await db.prepare()

        ladders = checkout / "forms" / "ladders"
        write_ladders(ladders, requirement=3)
        spec = ChannelsFile(
            channels=[
                ChannelSpec(
                    guild=GUILD,
                    channel=CHANNEL,
                    review_ladder="review",
                    quota_ladder="quota",
                )
            ]
        )

        async def start() -> None:
            async with db.write_session() as session:
                result = await sync_channels(session, spec, ladders_dir=ladders)
            print(f"  sync: created={result.created} ladders={result.ladders}")

        print("DEPLOY 1")
        await start()

        async with db.session() as session:
            group = (await ranks_repo.load_all_ladders(session))[GUILD]
            ladder_id = group.quota_ladder_id or 0
            binding = await repos.get_binding_by_channel(session, CHANNEL)
            print(f"  review ladder attached: {binding.rank_ladder_id is not None}")
            print(f"  quota ladder in config: {(binding.config or {}).get('quota_ladder')}")

        # The bot scrapes who is counted, into /data.
        async with db.write_session() as session:
            await repos.replace_roster(
                session,
                ladder_id,
                [
                    {
                        "user_id": 1,
                        "display_name": "Ann",
                        "tier_label": "Instructor",
                        "tier_ordinal": 10,
                        "required_per_week": 3,
                    }
                ],
            )
        print("  roster scraped: 1 person")

        # --- redeploy: wipe the checkout, re-upload identical files, keep /data ---
        print("\nDEPLOY 2 (unchanged files, checkout replaced)")
        shutil.rmtree(checkout)
        checkout = Path(tempfile.mkdtemp())
        ladders = checkout / "forms" / "ladders"
        write_ladders(ladders, requirement=3)

        await start()
        async with db.session() as session:
            group = (await ranks_repo.load_all_ladders(session))[GUILD]
            roster = await repos.list_roster(session, group.quota_ladder_id or 0)
            binding = await repos.get_binding_by_channel(session, CHANNEL)
        print(f"  roster survived: {[row.user_id for row in roster]}")
        print(f"  binding survived: {binding is not None}")
        assert [row.user_id for row in roster] == [1], "a redeploy lost the roster"
        assert binding is not None

        # --- redeploy with a changed quota ---
        print("\nDEPLOY 3 (quota changed 3 -> 5)")
        shutil.rmtree(checkout)
        checkout = Path(tempfile.mkdtemp())
        ladders = checkout / "forms" / "ladders"
        write_ladders(ladders, requirement=5)

        await start()
        async with db.session() as session:
            group = (await ranks_repo.load_all_ladders(session))[GUILD]
            quota_index = next(i for i in group.indexes if i.purpose == "quota")
        print(f"  new requirement in effect: {quota_index.quota_by_role[555]}")
        assert quota_index.quota_by_role[555] == 5

        print("\nOK: everything is rebuilt from git; /data keeps the roster")
    finally:
        await db.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(checkout, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

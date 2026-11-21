"""Verifies the ladders_loaded diagnostic against a real database.

The old log said review_roles=0 while approvals worked, because it counted from
`group.by_role`, a dict merged across ladders where a quota tier (no capabilities)
overwrites a review tier for the same role id.
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

from dsbot.config import Settings
from dsbot.modules.ranks.ladder import load_ladder
from dsbot.storage import ranks_repo
from dsbot.storage.db import Database


def review_role_count(group: object) -> int:
    """The same expression the bot now logs."""
    total = 0
    for index in group.indexes:  # type: ignore[attr-defined]
        if index.purpose != "review":
            continue
        total += sum(1 for entry in index.by_role.values() if "review" in entry[3])
    return total


async def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{(tmp / 'l.db').as_posix()}"))
    try:
        await db.prepare()
        async with db.write_session() as session:
            for name in ("review", "quota"):
                await ranks_repo.replace_ladder(
                    session, load_ladder(pathlib.Path(f"forms/ladders/{name}.yaml"))
                )

        async with db.session() as session:
            ladders = await ranks_repo.load_all_ladders(session)

        guild, group = next(iter(ladders.items()))
        print(f"guild {guild}")
        print(f"  ladders       : {[i.key for i in group.indexes]}")
        print(f"  merged roles  : {len(group.by_role)}")
        print(f"  review roles  : {review_role_count(group)}")

        old = sum(1 for caps in group.by_role.values() if caps and "review" in caps)
        print(f"  OLD expression: {old}   <- the misleading number")
        print(f"  NEW expression: {review_role_count(group)}")

        assert review_role_count(group) > 0, "the review ladder must contribute roles"
        print("\nOK: the diagnostic now reports the real number of review roles")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

"""Who to count: everyone holding a role from the rank ladder.

The roster is scraped from the **rank source guild**, not the report guild, because
instructors are not members of the server where the reports are read. That is the same
guild the resolver uses for approvals, so the two cannot drift apart.

Listing members by role needs Discord's privileged `GUILD_MEMBERS` intent. That is a
platform rule, not a choice: without the intent, the endpoint refuses and there is no
other way to learn who holds a role. Someone with zero reports must still appear as `0`,
so the intent is required for a correct count and its absence is reported as an error,
never silently worked around.

The scrape is stored in `roster_members`. A count then reads the stored roster instead
of Discord, so numbers are reproducible and a role change tomorrow does not rewrite
last week.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from dsbot.storage import repos
from dsbot.storage.db import Database

log = structlog.get_logger(__name__)


class RosterSource(Protocol):
    """Where role membership comes from. Implemented by Discord and by tests."""

    async def members_with_roles(self, guild_id: int) -> list[Any] | None:
        """Every guild member, or None when the roster cannot be read."""
        ...

    def supports_roster(self) -> bool:
        """Whether this source can list members at all."""
        ...


@dataclass(slots=True)
class ScrapeResult:
    members: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    total_members: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


class DiscordRosterSource:
    """Reads members through discord.py, which needs the Members intent.

    The intent is checked explicitly instead of being discovered from an exception:
    `guild.fetch_members` raises `ClientException`, and a bare exception would tell an
    operator nothing about the portal toggle they are missing.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def supports_roster(self) -> bool:
        intents = getattr(self._client, "intents", None)
        return bool(getattr(intents, "members", False))

    async def members_with_roles(self, guild_id: int) -> list[Any] | None:
        if not self.supports_roster():
            log.error(
                "roster_intent_missing",
                guild_id=guild_id,
                hint=(
                    "Enable SERVER MEMBERS INTENT in the Discord developer portal and set "
                    "DSBOT_ENABLE_MEMBERS_INTENT=true, then restart. Without it the bot "
                    "cannot see who holds a role, so people with no reports would be "
                    "missing from the count instead of showing 0."
                ),
            )
            return None
        guild = self._client.get_guild(guild_id)
        if guild is None:
            log.error("roster_guild_not_cached", guild_id=guild_id)
            return None
        try:
            return [member async for member in guild.fetch_members(limit=None)]
        except Exception:
            log.exception("roster_fetch_failed", guild_id=guild_id)
            return None


class StaticRosterSource:
    """Test double: a fixed member list."""

    def __init__(self, members: dict[int, list[Any]] | None = None, *, available: bool = True):
        self._members = members or {}
        self._available = available

    def supports_roster(self) -> bool:
        return self._available

    async def members_with_roles(self, guild_id: int) -> list[Any] | None:
        if not self._available:
            return None
        return self._members.get(guild_id, [])


class RosterService:
    """Scrape a roster and persist it, so counting never needs Discord."""

    def __init__(self, db: Database, source: RosterSource) -> None:
        self._db = db
        self._source = source

    async def scrape(
        self,
        guild_id: int,
        ladder: Any,
        *,
        requires: frozenset[str] = frozenset(),
        only_ladder_id: int | None = None,
    ) -> ScrapeResult:
        """Fetch role membership and reduce it to people on the ladder.

        `requires` selects the population: empty means "everyone who owes a report",
        `{"review"}` means "everyone who may approve". The two are deliberately
        separate, so a quota never silently includes the people who review.

        `only_ladder_id` restricts the scrape to one ladder. Without it, a guild that has
        both a review ladder and a quota ladder resolves people against every ladder at
        once - so somebody holding only a review role is matched, gets a review tier
        label, and appears in the weekly table with a quota of 0. The roster is the list
        of people who owe reports, so it must come from the quota ladder alone.
        """
        members = await self._source.members_with_roles(guild_id)
        if members is None:
            return ScrapeResult(
                error=(
                    "roster unavailable: the bot cannot list members of that guild. "
                    "Enable the Members intent, or count only people who already reported."
                )
            )

        rows: dict[int, dict[str, Any]] = {}
        for member in members:
            role_ids = frozenset(_role_ids(member))
            resolved = ladder.resolve(
                role_ids,
                _user_id(member),
                source="roster",
                requires=requires,
                only_ladder_id=only_ladder_id,
            )
            if resolved is None:
                continue
            # Highest quota tier wins: a promotion must not sum two requirements, and a
            # leftover junior role must not drag the duty back down.
            required, quota_label, quota_ordinal = ladder.required_for_roles(role_ids)
            rows[resolved.user_id] = {
                "user_id": resolved.user_id,
                "display_name": _display_name(member),
                "tier_key": resolved.tier_key,
                "tier_label": quota_label or resolved.tier_label or resolved.tier_key,
                "tier_ordinal": quota_ordinal if quota_ordinal is not None else resolved.ordinal,
                "required_per_week": required,
            }

        log.info(
            "roster_scraped",
            guild_id=guild_id,
            ladders=[index.key for index in ladder.indexes],
            scanned=len(members),
            matched=len(rows),
            requires=sorted(requires),
        )
        return ScrapeResult(members=list(rows.values()), total_members=len(members))

    async def refresh(
        self,
        guild_id: int,
        ladder: Any,
        *,
        ladder_id: int,
        requires: frozenset[str] = frozenset(),
    ) -> ScrapeResult:
        """Scrape and store. Returns the result, including the reason on failure."""
        result = await self.scrape(guild_id, ladder, requires=requires, only_ladder_id=ladder_id)
        if not result.ok:
            return result
        async with self._db.write_session() as session:
            await repos.replace_roster(session, ladder_id, result.members)
        return result

    async def load(self, ladder_id: int) -> list[dict[str, Any]]:
        async with self._db.session() as session:
            rows = await repos.list_roster(session, ladder_id)
            return [
                {
                    "user_id": row.user_id,
                    "display_name": row.display_name,
                    "tier_label": row.tier_label,
                    "tier_ordinal": row.tier_ordinal,
                    "required_per_week": row.required_per_week,
                    "captured_at": row.captured_at,
                }
                for row in rows
            ]


def _role_ids(member: Any) -> list[int]:
    roles = getattr(member, "roles", ()) or ()
    return [role.id for role in roles]


def _user_id(member: Any) -> int:
    return int(getattr(member, "id", 0))


def _display_name(member: Any) -> str:
    return (
        getattr(member, "display_name", None)
        or getattr(member, "name", None)
        or str(_user_id(member))
    )


__all__ = [
    "DiscordRosterSource",
    "RosterService",
    "RosterSource",
    "ScrapeResult",
    "StaticRosterSource",
]

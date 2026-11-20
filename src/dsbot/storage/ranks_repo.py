"""SQL for rank ladders. Everything that touches `rank_ladders` lives here."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsbot.modules.ranks.ladder import LadderSpec
from dsbot.storage.models import RankLadder, RankTier, RankTierRole, TierCapability


async def replace_ladder(session: AsyncSession, spec: LadderSpec) -> RankLadder:
    """Replace a guild's ladder with `spec`.

    Replacement, not merge: a ladder is a single authority ordering, and a partial
    update would leave stale tiers that still match roles. The old ladder for the same
    guild and key is removed first.
    """
    existing = await session.scalar(
        select(RankLadder).where(RankLadder.guild_id == spec.guild_id, RankLadder.key == spec.key)
    )
    if existing is not None:
        await session.execute(delete(RankLadder).where(RankLadder.id == existing.id))
        await session.flush()

    ladder = RankLadder(guild_id=spec.guild_id, key=spec.key, name=spec.name)
    session.add(ladder)
    await session.flush()

    for tier_spec in spec.tiers:
        tier = RankTier(
            ladder_id=ladder.id,
            key=tier_spec.key,
            label=tier_spec.label or tier_spec.key,
            ordinal=tier_spec.ordinal,
        )
        session.add(tier)
        await session.flush()
        for role_id in tier_spec.roles:
            session.add(RankTierRole(tier_id=tier.id, role_id=role_id))
        for capability in tier_spec.capabilities:
            session.add(TierCapability(tier_id=tier.id, capability=capability))

    await session.flush()
    return ladder


async def list_ladders(
    session: AsyncSession,
) -> Sequence[tuple[RankLadder, list[dict[str, Any]]]]:
    """Every ladder with its tiers, ready for a CLI table."""
    ladders = (await session.scalars(select(RankLadder).order_by(RankLadder.guild_id))).all()
    result: list[tuple[RankLadder, list[dict[str, Any]]]] = []
    for ladder in ladders:
        tiers = (
            await session.scalars(
                select(RankTier)
                .where(RankTier.ladder_id == ladder.id)
                .order_by(RankTier.ordinal.desc())
            )
        ).all()
        rows: list[dict[str, Any]] = []
        for tier in tiers:
            roles = (
                await session.scalars(
                    select(RankTierRole.role_id).where(RankTierRole.tier_id == tier.id)
                )
            ).all()
            caps = (
                await session.scalars(
                    select(TierCapability.capability).where(TierCapability.tier_id == tier.id)
                )
            ).all()
            rows.append(
                {
                    "key": tier.key,
                    "label": tier.label,
                    "ordinal": tier.ordinal,
                    "roles": list(roles),
                    "capabilities": list(caps),
                }
            )
        result.append((ladder, rows))
    return result


async def ladder_for_guild(session: AsyncSession, guild_id: int) -> RankLadder | None:
    """The ladder governing one guild. Used when building the live resolver."""
    return await session.scalar(select(RankLadder).where(RankLadder.guild_id == guild_id).limit(1))


async def load_all_ladders(session: AsyncSession) -> dict[int, Any]:
    """Build the guild -> LadderIndex map the resolver needs."""
    from dsbot.modules.ranks.resolver import LadderIndex

    ladders = (await session.scalars(select(RankLadder))).all()
    by_guild: dict[int, LadderIndex] = {}
    for ladder in ladders:
        tiers = (
            await session.scalars(select(RankTier).where(RankTier.ladder_id == ladder.id))
        ).all()
        by_role: dict[int, tuple[str, str, int, frozenset[str]]] = {}
        for tier in tiers:
            roles = (
                await session.scalars(
                    select(RankTierRole.role_id).where(RankTierRole.tier_id == tier.id)
                )
            ).all()
            caps = frozenset(
                (
                    await session.scalars(
                        select(TierCapability.capability).where(TierCapability.tier_id == tier.id)
                    )
                ).all()
            )
            for role_id in roles:
                by_role[role_id] = (tier.key, tier.label or tier.key, tier.ordinal, caps)
        by_guild[ladder.guild_id] = LadderIndex(
            ladder_id=ladder.id,
            guild_id=ladder.guild_id,
            key=ladder.key,
            by_role=by_role,
        )
    return by_guild

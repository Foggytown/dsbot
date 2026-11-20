"""Rank resolution across servers.

The hard case: an approver from the specialization server reacts in the report server.
discord.py gives us a User with no roles, so the rank must be looked up in their home
guild. These tests pin that behaviour without a live gateway.
"""

from __future__ import annotations

from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.ranks.resolver import LadderIndex, RankResolver
from dsbot.modules.ranks.source import StaticRankSource

SPEC = LadderSpec(
    key="instructors",
    guild_id=900,
    name="Instructors",
    tiers=[
        TierSpec(
            key="head",
            label="Head Instructor",
            ordinal=50,
            roles=[10],
            capabilities=["review", "admin"],
        ),
        TierSpec(
            key="senior", label="Senior Instructor", ordinal=30, roles=[20], capabilities=["review"]
        ),
        TierSpec(key="junior", label="Junior", ordinal=10, roles=[30]),
    ],
)


def index_from(spec: LadderSpec) -> dict[int, LadderIndex]:
    return {
        spec.guild_id: LadderIndex(
            ladder_id=1,
            guild_id=spec.guild_id,
            key=spec.key,
            by_role={
                role: (t.key, t.label, t.ordinal, frozenset(t.capabilities))
                for t in spec.tiers
                for role in t.roles
            },
        )
    }


def resolver(
    mapping: dict[tuple[int, int], frozenset[int]],
) -> tuple[RankResolver, StaticRankSource]:
    source = StaticRankSource(mapping)
    return RankResolver(source, index_from(SPEC)), source


async def test_known_role_resolves_to_its_tier() -> None:
    res, _ = resolver({(900, 1): frozenset({20})})
    rank = await res.resolve(900, 1)
    assert rank is not None
    assert rank.tier_key == "senior"
    assert rank.ordinal == 30
    assert rank.can_review is True
    assert rank.can_admin is False


async def test_highest_role_wins_when_several_match() -> None:
    res, _ = resolver({(900, 1): frozenset({10, 20, 30})})
    rank = await res.resolve(900, 1)
    assert rank is not None
    assert rank.tier_key == "head"


async def test_tier_without_review_cannot_review() -> None:
    res, _ = resolver({(900, 1): frozenset({30})})
    rank = await res.resolve(900, 1)
    assert rank is not None
    assert rank.can_review is False


async def test_user_with_no_configured_role_is_unresolved() -> None:
    res, _ = resolver({(900, 1): frozenset({999})})
    assert await res.resolve(900, 1) is None
    assert res.unresolved == 1


async def test_non_member_is_unresolved() -> None:
    res, _ = resolver({})
    assert await res.resolve(900, 1) is None


async def test_unknown_guild_is_unresolved_without_calling_the_source() -> None:
    res, source = resolver({(900, 1): frozenset({10})})
    assert await res.resolve(111, 1) is None
    assert source.calls == []


async def test_second_call_is_cached() -> None:
    res, source = resolver({(900, 1): frozenset({10})})
    await res.resolve(900, 1)
    await res.resolve(900, 1)
    assert len(source.calls) == 1
    assert res.hits == 1


async def test_unresolved_result_is_cached_too() -> None:
    """Otherwise every reaction from a non-member costs a REST call."""
    res, source = resolver({})
    await res.resolve(900, 1)
    await res.resolve(900, 1)
    assert len(source.calls) == 1


async def test_invalidate_forces_a_fresh_lookup() -> None:
    res, source = resolver({(900, 1): frozenset({10})})
    await res.resolve(900, 1)
    res.invalidate(900, 1)
    await res.resolve(900, 1)
    assert len(source.calls) == 2


async def test_ladder_change_clears_the_cache() -> None:
    res, source = resolver({(900, 1): frozenset({10})})
    await res.resolve(900, 1)
    res.set_ladders(index_from(SPEC))
    await res.resolve(900, 1)
    assert len(source.calls) == 2


async def test_ranks_are_per_guild() -> None:
    """The same user can hold different authority in different servers."""
    other = LadderSpec(
        key="other",
        guild_id=901,
        tiers=[TierSpec(key="only", label="Only", ordinal=5, roles=[77], capabilities=["review"])],
    )
    source = StaticRankSource({(900, 1): frozenset({10}), (901, 1): frozenset({77})})
    ladders = index_from(SPEC)
    ladders.update(index_from(other))
    res = RankResolver(source, ladders)

    assert (await res.resolve(900, 1)).tier_key == "head"  # type: ignore[union-attr]
    assert (await res.resolve(901, 1)).tier_key == "only"  # type: ignore[union-attr]


async def test_stats_report_cache_behaviour() -> None:
    res, _ = resolver({(900, 1): frozenset({10}), (900, 2): frozenset({20})})
    await res.resolve(900, 1)
    await res.resolve(900, 1)
    await res.resolve(900, 2)
    stats = res.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 2

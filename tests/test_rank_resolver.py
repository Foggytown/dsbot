"""Rank resolution across servers.

The hard case: an approver from the specialization server reacts in the report server.
discord.py gives us a User with no roles, so the rank must be looked up in their home
guild. These tests pin that behaviour without a live gateway.
"""

from __future__ import annotations

from dsbot.modules.ranks.ladder import LadderSpec, TierSpec
from dsbot.modules.ranks.resolver import GuildLadders, LadderIndex, RankResolver
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


def index_from(spec: LadderSpec) -> dict[int, GuildLadders]:
    group = GuildLadders(guild_id=spec.guild_id)
    group.add(
        LadderIndex(
            ladder_id=1,
            guild_id=spec.guild_id,
            key=spec.key,
            by_role={
                role: (t.key, t.label, t.ordinal, frozenset(t.capabilities))
                for t in spec.tiers
                for role in t.roles
            },
        )
    )
    return {spec.guild_id: group}


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


async def test_review_lookup_ignores_a_quota_tier_even_when_it_is_senior() -> None:
    """Roles that owe reports and roles that approve are different populations.

    A quota tier may legitimately have a high ordinal; it still must not decide a
    verdict. Asking for `review` is what separates the two, not the ordinal.
    """
    group = GuildLadders(guild_id=900)
    group.add(
        LadderIndex(
            ladder_id=1,
            guild_id=900,
            key="instructors",
            by_role={
                # A reviewer at 30.
                20: ("senior", "Senior Instructor", 30, frozenset({"review"})),
                # A reporting tier with a higher ordinal and no capability.
                30: ("squadlead", "Squad Lead", 90, frozenset()),
            },
        )
    )
    res = RankResolver(StaticRankSource({(900, 1): frozenset({20, 30})}), {900: group})

    # For approval, the quota tier is invisible.
    review = await res.resolve(900, 1, requires=frozenset({"review"}))
    assert review is not None
    assert review.tier_key == "senior"

    # For counting, the highest ordinal still wins.
    any_rank = await res.resolve(900, 1)
    assert any_rank is not None
    assert any_rank.tier_key == "squadlead"


async def test_a_pure_quota_tier_cannot_approve_at_all() -> None:
    group = GuildLadders(guild_id=900)
    group.add(
        LadderIndex(
            ladder_id=2,
            guild_id=900,
            key="instructors",
            by_role={40: ("instructor", "Instructor", 10, frozenset())},
        )
    )
    res = RankResolver(StaticRankSource({(900, 1): frozenset({40})}), {900: group})
    assert await res.resolve(900, 1, requires=frozenset({"review"})) is None
    assert (await res.resolve(900, 1)) is not None


async def test_two_ladders_in_one_guild_are_kept_apart() -> None:
    """A review ladder and a quota ladder must coexist without sharing roles."""
    group = GuildLadders(guild_id=900)
    group.add(
        LadderIndex(
            ladder_id=1,
            guild_id=900,
            key="review",
            by_role={10: ("head", "Head", 100, frozenset({"review"}))},
        )
    )
    group.add(
        LadderIndex(
            ladder_id=2,
            guild_id=900,
            key="quota",
            by_role={99: ("instructor", "Instructor", 10, frozenset())},
        )
    )
    res = RankResolver(
        StaticRankSource({(900, 1): frozenset({10}), (900, 2): frozenset({99})}),
        {900: group},
    )
    assert (await res.resolve(900, 1, requires=frozenset({"review"}))).tier_key == "head"  # type: ignore[union-attr]
    assert (await res.resolve(900, 2, requires=frozenset({"review"}))) is None
    assert (await res.resolve(900, 2)).tier_key == "instructor"  # type: ignore[union-attr]


async def test_pinning_a_channel_ladder_restricts_the_answer() -> None:
    """Channel-level hierarchy: one channel may be ordered by its own ladder only."""
    group = GuildLadders(guild_id=900)
    group.add(
        LadderIndex(
            ladder_id=1,
            guild_id=900,
            key="alpha",
            by_role={10: ("cmd_a", "Commander A", 50, frozenset({"review"}))},
        )
    )
    group.add(
        LadderIndex(
            ladder_id=2,
            guild_id=900,
            key="bravo",
            by_role={10: ("cmd_b", "Commander B", 10, frozenset({"review"}))},
        )
    )
    res = RankResolver(StaticRankSource({(900, 1): frozenset({10})}), {900: group})

    # Unpinned: the highest ordinal across the guild wins.
    assert (await res.resolve(900, 1)).tier_key == "cmd_a"  # type: ignore[union-attr]
    # Pinned to bravo: the same person is a lower tier in that channel's ordering.
    pinned = await res.resolve(900, 1, ladder_id=2)
    assert pinned is not None
    assert pinned.tier_key == "cmd_b"
    assert pinned.ordinal == 10


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

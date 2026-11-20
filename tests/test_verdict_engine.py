"""Verdict engine tests: every rule, including the ones that only matter in edge cases.

The un-react case is the reason this is a pure function over state, so it gets the most
attention.
"""

from __future__ import annotations

from dsbot.modules.verdict.engine import (
    APPROVE_EMOJI,
    REJECT_EMOJI,
    Rank,
    Reaction,
    classify_emoji,
    compute_verdict,
)

CHECK = "\u2705"
CROSS = "\u274c"
THUMBS = "\U0001f44d"


def approval(user: int) -> Reaction:
    return Reaction(user_id=user, emoji_name=CHECK)


def rejection(user: int) -> Reaction:
    return Reaction(user_id=user, emoji_name=CROSS)


def rank(user: int, ordinal: int, label: str | None = None) -> Rank:
    return Rank(user_id=user, ordinal=ordinal, tier_label=label)


RANKS = {
    1: rank(1, 100, "High Command"),
    2: rank(2, 50, "Head Instructor"),
    3: rank(3, 30, "Senior Instructor"),
    4: rank(4, 10, "Instructor"),  # no review capability in the ladder, so unreachable
}


def test_no_reactions_is_pending() -> None:
    result = compute_verdict([], RANKS)
    assert result.verdict == "pending"
    assert result.by_user_id is None


def test_single_approval() -> None:
    result = compute_verdict([approval(2)], RANKS)
    assert result.verdict == "approved"
    assert result.by_user_id == 2
    assert result.by_ordinal == 50


def test_single_rejection() -> None:
    result = compute_verdict([rejection(3)], RANKS)
    assert result.verdict == "rejected"
    assert result.by_user_id == 3


def test_higher_rank_approval_beats_lower_rank_rejection() -> None:
    result = compute_verdict([rejection(3), approval(2)], RANKS)
    assert result.verdict == "approved"
    assert result.by_ordinal == 50


def test_higher_rank_rejection_beats_lower_rank_approval() -> None:
    result = compute_verdict([approval(3), rejection(1)], RANKS)
    assert result.verdict == "rejected"
    assert result.by_ordinal == 100


def test_same_rank_disagreement_is_a_conflict() -> None:
    """No automatic answer is safe when equal authority disagrees."""
    result = compute_verdict([approval(2), rejection(5)], {2: rank(2, 50), 5: rank(5, 50)})
    assert result.verdict == "conflict"
    assert set(result.conflicted_users) == {2, 5}


def test_same_rank_agreement_is_not_a_conflict() -> None:
    result = compute_verdict([approval(2), approval(5)], {2: rank(2, 50), 5: rank(5, 50)})
    assert result.verdict == "approved"


def test_removing_the_deciding_reaction_reverts_to_the_next_rank() -> None:
    """The whole point of recomputing from state instead of replaying events."""
    both = [rejection(3), approval(2)]
    assert compute_verdict(both, RANKS).verdict == "approved"

    # The head instructor withdraws, leaving the senior instructor's rejection.
    after = [rejection(3)]
    assert compute_verdict(after, RANKS).verdict == "rejected"


def test_removing_all_reactions_returns_to_pending() -> None:
    assert compute_verdict([approval(2)], RANKS).verdict == "approved"
    assert compute_verdict([], RANKS).verdict == "pending"


def test_unknown_rank_is_ignored_not_guessed() -> None:
    result = compute_verdict([approval(99)], RANKS)
    assert result.verdict == "pending"
    assert (99, "rank_unknown") in result.ignored


def test_unrelated_emoji_is_ignored() -> None:
    result = compute_verdict([Reaction(user_id=2, emoji_name=THUMBS)], RANKS)
    assert result.verdict == "pending"
    assert (2, "emoji_ignored") in result.ignored


def test_ignored_reactions_do_not_block_a_real_one() -> None:
    result = compute_verdict([Reaction(user_id=2, emoji_name=THUMBS), approval(3)], RANKS)
    assert result.verdict == "approved"
    assert result.by_user_id == 3


def test_counted_list_is_ordered_by_authority() -> None:
    result = compute_verdict([approval(3), approval(2)], RANKS)
    assert [r.ordinal for r in result.counted] == [50, 30]


def test_payload_is_json_safe() -> None:
    import json

    result = compute_verdict([approval(2), rejection(5)], {2: rank(2, 50), 5: rank(5, 50)})
    payload = result.to_payload()
    json.dumps(payload)
    assert payload["verdict"] == "conflict"


def test_emoji_classification() -> None:
    assert classify_emoji(CHECK) == "approve"
    assert classify_emoji(CROSS) == "reject"
    assert classify_emoji(THUMBS) == "ignore"
    assert classify_emoji(None) == "ignore"


def test_both_emoji_sets_are_known() -> None:
    assert CHECK in APPROVE_EMOJI
    assert CROSS in REJECT_EMOJI
    assert not (APPROVE_EMOJI & REJECT_EMOJI)


def test_single_user_reacting_both_ways_higher_emoji_wins_by_order() -> None:
    """A user who both approves and rejects: their own rank cannot outrank itself."""
    result = compute_verdict([approval(2), rejection(2)], RANKS)
    # Same ordinal on both sides, so this is a conflict rather than an arbitrary pick.
    assert result.verdict == "conflict"

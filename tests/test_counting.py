"""What counts as a report, and for whom.

These pin the rules that were agreed with the community, because each one is a place
where a "reasonable" alternative would produce a wrong number:

  * an undecided report counts as approved (the instructor is not penalised for a
    reviewer's backlog), but is also reported separately so the backlog stays visible;
  * a conflict never counts;
  * someone with no reports still appears, with 0.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dsbot.modules.quotas.counting import (
    CountedReport,
    MemberTally,
    RosterEntry,
    evaluate_reports,
    tally,
)
from dsbot.modules.verdict.engine import Rank, Reaction

CHECK = "\u2705"
CROSS = "\u274c"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

REVIEWER = 500
HEAD = 501


def rank_map(user_id: int, ordinal: int) -> dict[int, Rank | None]:
    return {user_id: Rank(user_id=user_id, ordinal=ordinal, tier_label="Tier")}


def report(
    report_id: int,
    author: int,
    *,
    reactions: tuple[Reaction, ...] = (),
    ranks: dict[int, Rank | None] | None = None,
) -> CountedReport:
    return CountedReport(
        report_id=report_id,
        author_user_id=author,
        created_at=NOW,
        reactions=reactions,
        ranks=ranks or {},
    )


def test_approved_report_counts_once() -> None:
    outcomes = evaluate_reports(
        [
            report(
                1,
                10,
                reactions=(Reaction(user_id=REVIEWER, emoji_name=CHECK),),
                ranks=rank_map(REVIEWER, 30),
            )
        ]
    )
    summary = tally(outcomes, [RosterEntry(user_id=10, display_name="Ann", required_count=3)])
    row = summary.tallies[0]
    assert row.approved_count == 1
    assert row.pending_count == 0
    assert row.met is False  # 1 of 3


def test_undecided_report_counts_as_approved_but_is_visible_as_pending() -> None:
    """The reviewer's backlog must not cost the instructor a report."""
    outcomes = evaluate_reports([report(1, 10)])
    summary = tally(outcomes, [RosterEntry(user_id=10, required_count=1)])
    row = summary.tallies[0]
    assert row.pending_count == 1
    assert row.approved_count == 0
    assert row.met is True  # 1 credited of 1 required
    assert summary.credited_total() == 1
    assert summary.pending_total() == 1


def test_rejected_report_does_not_count() -> None:
    outcomes = evaluate_reports(
        [
            report(
                1,
                10,
                reactions=(Reaction(user_id=REVIEWER, emoji_name=CROSS),),
                ranks=rank_map(REVIEWER, 30),
            )
        ]
    )
    summary = tally(outcomes, [RosterEntry(user_id=10, required_count=1)])
    assert summary.tallies[0].rejected_count == 1
    assert summary.tallies[0].met is False


def test_conflict_never_counts() -> None:
    """Equal authority disagreeing is a staff problem, not an approval."""
    outcomes = evaluate_reports(
        [
            report(
                1,
                10,
                reactions=(
                    Reaction(user_id=REVIEWER, emoji_name=CHECK),
                    Reaction(user_id=HEAD, emoji_name=CROSS),
                ),
                ranks={
                    REVIEWER: Rank(user_id=REVIEWER, ordinal=30),
                    HEAD: Rank(user_id=HEAD, ordinal=30),
                },
            )
        ]
    )
    summary = tally(outcomes, [RosterEntry(user_id=10, required_count=1)])
    assert summary.tallies[0].conflict_count == 1
    assert summary.tallies[0].approved_count == 0
    assert summary.tallies[0].met is False
    assert summary.conflict_total() == 1


def test_higher_rank_wins_between_different_tiers() -> None:
    outcomes = evaluate_reports(
        [
            report(
                1,
                10,
                reactions=(
                    Reaction(user_id=REVIEWER, emoji_name=CHECK),
                    Reaction(user_id=HEAD, emoji_name=CROSS),
                ),
                ranks={
                    REVIEWER: Rank(user_id=REVIEWER, ordinal=30),
                    HEAD: Rank(user_id=HEAD, ordinal=50),
                },
            )
        ]
    )
    assert outcomes[0].verdict == "rejected"


def test_author_reacting_on_their_own_report_can_approve_it() -> None:
    """A senior instructor may approve their own report; that is expected, not a bug."""
    outcomes = evaluate_reports(
        [
            report(
                1,
                10,
                reactions=(Reaction(user_id=10, emoji_name=CHECK),),
                ranks=rank_map(10, 30),
            )
        ]
    )
    assert outcomes[0].verdict == "approved"


def test_someone_with_zero_reports_still_appears() -> None:
    """'Nobody reported' and 'nobody is tracked' must not look the same."""
    outcomes = evaluate_reports([report(1, 10)])
    summary = tally(
        outcomes,
        [
            RosterEntry(user_id=10, display_name="Ann", required_count=1),
            RosterEntry(user_id=11, display_name="Bob", required_count=1),
        ],
    )
    names = {row.display_name: row for row in summary.tallies}
    assert names["Bob"].approved_count == 0
    assert names["Bob"].pending_count == 0
    assert names["Bob"].met is False


def test_report_by_an_unranked_author_is_kept_separately() -> None:
    """A missing role is an admin mistake; the work must not vanish."""
    outcomes = evaluate_reports([report(1, 999)])
    summary = tally(outcomes, [RosterEntry(user_id=10, required_count=1)])
    assert len(summary.unranked) == 1
    assert summary.unranked[0].user_id == 999
    assert summary.credited_total() == 1


def test_unranked_authors_are_not_counted_once_per_report() -> None:
    outcomes = evaluate_reports([report(1, 999), report(2, 999), report(3, 999)])
    summary = tally(outcomes, [])
    assert len(summary.unranked) == 1
    assert summary.unranked[0].pending_count == 3


def test_quota_met_uses_approved_plus_pending() -> None:
    row = MemberTally(user_id=1, approved_count=2, pending_count=1, required_count=3)
    assert row.met is True
    row.rejected_count = 5
    assert row.met is True


def test_no_quota_is_not_a_passed_quota() -> None:
    """0 of 0 must never look like success; there is no requirement to meet."""
    row = MemberTally(user_id=1, approved_count=0, required_count=0)
    assert row.met is False
    assert MemberTally(user_id=1, approved_count=5, required_count=0).met is False


def test_everyone_is_sorted_by_volume_then_name() -> None:
    outcomes = evaluate_reports(
        [
            report(
                1,
                10,
                reactions=(Reaction(user_id=REVIEWER, emoji_name=CHECK),),
                ranks=rank_map(REVIEWER, 30),
            )
        ]
    )
    summary = tally(
        outcomes,
        [
            RosterEntry(user_id=11, display_name="Bob"),
            RosterEntry(user_id=10, display_name="Ann"),
        ],
    )
    assert [row.display_name for row in summary.everyone()] == ["Ann", "Bob"]

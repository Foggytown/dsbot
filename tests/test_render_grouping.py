"""Presentation rules staff asked for:

* group people by tier, because that is how they think about them;
* allow hiding a noisy tier from the message without changing who is counted;
* ping people instead of showing a nickname;
* show approvals that cannot be verified separately from both "approved" and "pending".
"""

from __future__ import annotations

from dsbot.modules.quotas.counting import CountSummary, MemberTally
from dsbot.modules.quotas.render import (
    RenderContext,
    group_by_tier,
    member_line,
    render_csv,
    render_message,
)

CONTEXT = RenderContext(period_label="16.11–22.11", timezone="Europe/Moscow")


def summary(*rows: MemberTally, unranked: tuple[MemberTally, ...] = ()) -> CountSummary:
    return CountSummary(tallies=list(rows), unranked=list(unranked))


def test_rows_are_grouped_by_tier_highest_first() -> None:
    rows = [
        MemberTally(user_id=1, display_name="A", tier_label="Instructor", tier_ordinal=10),
        MemberTally(user_id=2, display_name="B", tier_label="Head", tier_ordinal=50),
        MemberTally(user_id=3, display_name="C", tier_label="Instructor", tier_ordinal=10),
    ]
    groups = group_by_tier(rows)
    assert [label for label, _ in groups] == ["Head", "Instructor"]
    assert len(groups[1][1]) == 2


def test_a_tier_heading_appears_once_in_the_message() -> None:
    rows = [
        MemberTally(user_id=1, display_name="A", tier_label="Instructor", tier_ordinal=10),
        MemberTally(user_id=2, display_name="B", tier_label="Instructor", tier_ordinal=10),
    ]
    page = render_message(summary(*rows), CONTEXT)[0]
    assert page.count("Instructor") == 1


def test_people_without_a_tier_are_grouped_together() -> None:
    groups = group_by_tier([MemberTally(user_id=1, display_name="A")])
    assert groups[0][0] == "Без тира"


def test_within_a_tier_more_reports_come_first() -> None:
    rows = [
        MemberTally(
            user_id=1, display_name="Low", tier_label="T", tier_ordinal=1, approved_count=1
        ),
        MemberTally(
            user_id=2, display_name="High", tier_label="T", tier_ordinal=1, approved_count=5
        ),
    ]
    members = group_by_tier(rows)[0][1]
    assert [m.display_name for m in members] == ["High", "Low"]


def test_a_hidden_tier_disappears_from_the_message() -> None:
    rows = [
        MemberTally(user_id=1, display_name="A", tier_label="Instructor", tier_ordinal=10),
        MemberTally(user_id=2, display_name="B", tier_label="Исключение", tier_ordinal=30),
    ]
    hidden = RenderContext(
        period_label="16.11–22.11",
        timezone="Europe/Moscow",
        hidden_tiers=("Исключение",),
    )
    page = render_message(summary(*rows), hidden)[0]
    assert "Instructor" in page
    assert "Исключение" not in page


def test_hiding_is_case_insensitive() -> None:
    rows = [MemberTally(user_id=1, display_name="A", tier_label="Instructor", tier_ordinal=10)]
    hidden = RenderContext(
        period_label="16.11–22.11",
        timezone="Europe/Moscow",
        hidden_tiers=("instructor",),
    )
    page = render_message(summary(*rows), hidden)[0]
    assert "Instructor" not in page


def test_hiding_every_tier_says_so_instead_of_showing_nothing() -> None:
    rows = [MemberTally(user_id=1, display_name="A", tier_label="Instructor", tier_ordinal=10)]
    hidden = RenderContext(
        period_label="16.11–22.11",
        timezone="Europe/Moscow",
        hidden_tiers=("Instructor",),
    )
    page = render_message(summary(*rows), hidden)[0]
    assert "скрыты" in page


def test_people_are_shown_as_pings() -> None:
    row = MemberTally(user_id=12345, display_name="Ann", approved_count=1, required_count=1)
    assert "<@12345>" in member_line(row)


def test_a_ping_is_shown_even_without_a_stored_name() -> None:
    row = MemberTally(user_id=99, approved_count=1)
    assert "<@99>" in member_line(row)


def test_unknown_approval_is_its_own_category() -> None:
    row = MemberTally(user_id=1, display_name="A", unknown_approval_count=2, required_count=3)
    line = member_line(row)
    assert "без роли 2" in line
    assert "не проверено" not in line


def test_unknown_approval_counts_toward_the_quota() -> None:
    row = MemberTally(user_id=1, unknown_approval_count=3, required_count=3)
    assert row.credited_total == 3
    assert row.met is True


def test_csv_separates_the_unknown_category() -> None:
    row = MemberTally(user_id=1, display_name="A", unknown_approval_count=2, required_count=2)
    text = render_csv(summary(row))
    assert "approved_unknown" in text.splitlines()[0]
    assert ",2," in text


def test_going_over_the_quota_shows_the_real_total() -> None:
    """'4/1' says exactly what happened; '1/1 +3' makes the reader add it up."""
    row = MemberTally(user_id=1, approved_count=4, required_count=1)
    line = member_line(row)
    assert "4/1" in line
    assert "+" not in line


def test_the_total_matches_the_rows_and_ignores_hidden_tiers() -> None:
    rows = [
        MemberTally(user_id=1, display_name="A", tier_label="T", tier_ordinal=1, approved_count=2),
        MemberTally(user_id=2, display_name="B", tier_label="H", tier_ordinal=9, approved_count=1),
    ]
    hidden = RenderContext(
        period_label="16.11–22.11",
        timezone="Europe/Moscow",
        hidden_tiers=("H",),
    )
    page = render_message(summary(*rows), hidden)[0]
    assert "учтено: **2**" in page

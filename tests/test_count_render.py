"""What staff actually see: wording, markers, paging, CSV.

The point of these tests is the awkward cases. A table that looks fine when everything
works can still be misleading when the roster is empty or someone has zero reports.
"""

from __future__ import annotations

from dsbot.modules.quotas.counting import CountSummary, MemberTally
from dsbot.modules.quotas.render import RenderContext, member_line, render_csv, render_message

CONTEXT = RenderContext(
    title="Недельные нормы", period_label="16.11–22.11", timezone="Europe/Moscow"
)


def summary(*rows: MemberTally, unranked: tuple[MemberTally, ...] = ()) -> CountSummary:
    return CountSummary(tallies=list(rows), unranked=list(unranked))


def test_met_and_short_markers_differ() -> None:
    met = MemberTally(user_id=1, display_name="Ann", approved_count=3, required_count=3)
    short = MemberTally(user_id=2, display_name="Bob", approved_count=1, required_count=3)
    assert "✅" in member_line(met)
    assert "⚠️" in member_line(short)


def test_line_shows_total_first_then_name() -> None:
    row = MemberTally(
        user_id=1, display_name="Ann", approved_count=2, pending_count=1, required_count=3
    )
    line = member_line(row)
    assert line.index("3/3") < line.index("Ann")
    assert "ждёт проверки 1" in line


def test_zero_reports_is_shown_explicitly() -> None:
    row = MemberTally(user_id=1, display_name="Bob", required_count=2)
    assert "0/2" in member_line(row)


def test_without_a_quota_only_the_total_is_shown() -> None:
    row = MemberTally(user_id=1, display_name="Ann", approved_count=4)
    line = member_line(row)
    assert "4" in line
    assert "/" not in line


def test_empty_roster_says_so_instead_of_showing_an_empty_table() -> None:
    pages = render_message(summary(), CONTEXT)
    assert len(pages) == 1
    assert "Никого" in pages[0]


def test_header_states_the_period_and_timezone() -> None:
    page = render_message(summary(), CONTEXT)[0]
    assert CONTEXT.period_label in page
    assert CONTEXT.timezone in page


def test_conflict_is_called_out_in_the_header() -> None:
    row = MemberTally(user_id=1, display_name="Ann", required_count=1, conflict_count=2)
    page = render_message(summary(row), CONTEXT)[0]
    assert "конфликт" in page.lower()


def test_long_rosters_are_split_into_several_messages() -> None:
    rows = [
        MemberTally(user_id=i, display_name=f"Person number {i}", required_count=1)
        for i in range(60)
    ]
    pages = render_message(summary(*rows), CONTEXT)
    assert len(pages) > 1
    for page in pages:
        assert len(page) <= 2000


def test_unranked_authors_are_flagged_with_a_role_hint() -> None:
    unranked = MemberTally(user_id=77, approved_count=2)
    page = render_message(summary(unranked=(unranked,)), CONTEXT)[0]
    assert "77" in page
    assert "роль" in page


def test_csv_has_a_bom_for_excel_and_all_columns() -> None:
    row = MemberTally(
        user_id=1,
        display_name="Анна",
        tier_label="Instructor",
        tier_ordinal=10,
        approved_count=2,
        required_count=3,
    )
    text = render_csv(summary(row))
    assert text.startswith("\ufeff")
    header = text.lstrip("\ufeff").splitlines()[0]
    assert header.startswith("user_id,display_name")
    assert "Анна" in text


def test_csv_marks_met_and_short() -> None:
    met = MemberTally(user_id=1, display_name="Ann", approved_count=3, required_count=3)
    short = MemberTally(user_id=2, display_name="Bob", approved_count=1, required_count=3)
    lines = render_csv(summary(met, short)).lstrip("\ufeff").splitlines()
    assert lines[1].endswith("yes")
    assert lines[2].endswith("no")

"""Week boundaries.

An off-by-one here shifts every number by a whole report, and the mistake is invisible
in the output, so the boundaries get explicit tests: the first and last second of a
week, the timezone offset, and a year boundary.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from dsbot.modules.quotas.period import (
    Period,
    current_week,
    explicit_period,
    load_timezone,
    previous_week,
    week_of,
    weeks_back,
)

MOSCOW = "Europe/Moscow"  # UTC+3, no DST


def test_week_starts_monday_midnight_local_time() -> None:
    # Wednesday 18 Nov 2026, noon UTC.
    period = week_of(datetime(2026, 11, 18, 12, tzinfo=UTC), MOSCOW)
    # Monday 16 Nov 00:00 Moscow == Sunday 15 Nov 21:00 UTC.
    assert period.start == datetime(2026, 11, 15, 21, 0, tzinfo=UTC)
    assert period.end == datetime(2026, 11, 22, 21, 0, tzinfo=UTC)


def test_first_second_of_the_week_is_inside() -> None:
    period = week_of(datetime(2026, 11, 18, 12, tzinfo=UTC), MOSCOW)
    assert period.contains(period.start)


def test_last_second_of_the_week_is_inside() -> None:
    period = week_of(datetime(2026, 11, 18, 12, tzinfo=UTC), MOSCOW)
    from datetime import timedelta

    assert period.contains(period.end - timedelta(seconds=1))


def test_the_next_monday_is_outside() -> None:
    """Half-open intervals: the boundary belongs to exactly one week."""
    period = week_of(datetime(2026, 11, 18, 12, tzinfo=UTC), MOSCOW)
    assert not period.contains(period.end)
    following = week_of(period.end, MOSCOW)
    assert following.start == period.end


def test_previous_week_is_seven_days_earlier() -> None:
    current = current_week(datetime(2026, 11, 18, 12, tzinfo=UTC), MOSCOW)
    previous = previous_week(datetime(2026, 11, 18, 12, tzinfo=UTC), MOSCOW)
    assert previous.end == current.start
    assert (current.start - previous.start).days == 7


def test_weeks_back_spans_consecutive_weeks() -> None:
    period = weeks_back(3, datetime(2026, 11, 18, 12, tzinfo=UTC), MOSCOW)
    current = current_week(datetime(2026, 11, 18, 12, tzinfo=UTC), MOSCOW)
    assert period.end == current.end
    assert period.days == 21


def test_weeks_back_rejects_zero() -> None:
    with pytest.raises(ValueError):
        weeks_back(0)


def test_year_boundary_week() -> None:
    """1 Jan 2027 falls in the ISO week starting Monday 28 Dec 2026."""
    period = week_of(datetime(2027, 1, 1, 12, tzinfo=UTC), MOSCOW)
    assert period.start == datetime(2026, 12, 27, 21, 0, tzinfo=UTC)


def test_explicit_period_includes_the_whole_end_day() -> None:
    period = explicit_period(date(2026, 11, 10), date(2026, 11, 16), MOSCOW)
    assert period.start == datetime(2026, 11, 9, 21, 0, tzinfo=UTC)
    assert period.end == datetime(2026, 11, 16, 21, 0, tzinfo=UTC)


def test_explicit_period_rejects_an_inverted_range() -> None:
    with pytest.raises(ValueError):
        Period(start=datetime(2026, 11, 16, tzinfo=UTC), end=datetime(2026, 11, 10, tzinfo=UTC))


def test_unknown_timezone_is_an_error_not_a_silent_utc() -> None:
    with pytest.raises(ValueError):
        load_timezone("Mars/Olympus")


def test_label_shows_the_local_last_day() -> None:
    period = week_of(datetime(2026, 11, 18, 12, tzinfo=UTC), MOSCOW)
    label = period.label(MOSCOW)
    assert label.startswith("16.11.2026")
    # The last instant of the week is Sunday 22 Nov in Moscow, not the 23rd.
    assert label.endswith("22.11.2026")

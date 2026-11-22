"""The close schedule: parsing, rolling, and when a period is due.

Pure functions, so these tests need no database and no clock: every case passes an explicit
`now`, because a test that depends on the current date fails on a Tuesday.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dsbot.modules.quotas.schedule import (
    DEFAULT_GRACE,
    Schedule,
    describe_close,
    due_at,
    parse_close,
)

MOSCOW = "Europe/Moscow"


def local(text: str) -> datetime:
    """A Moscow wall-clock moment as UTC, to keep the assertions readable."""
    return parse_close(text, MOSCOW)


def test_the_format_an_admin_types_is_understood() -> None:
    """`/results set next_result:22.11.2026 23:59:59` must not be the spelling that fails."""
    moment = parse_close("22.11.2026 23:59:59", MOSCOW)
    # Moscow is UTC+3, so 23:59:59 local is 20:59:59 UTC.
    assert moment == datetime(2026, 11, 22, 20, 59, 59, tzinfo=UTC)


def test_a_bare_date_means_the_end_of_that_day() -> None:
    """'the period ends on the 22nd' must include the 22nd, not stop at midnight."""
    moment = parse_close("22.11.2026", MOSCOW)
    assert moment == datetime(2026, 11, 22, 20, 59, 59, tzinfo=UTC)


def test_hours_and_minutes_without_seconds_work() -> None:
    assert parse_close("22.11.2026 23:59", MOSCOW) == datetime(2026, 11, 22, 20, 59, tzinfo=UTC)


def test_iso_form_is_accepted_too() -> None:
    assert parse_close("2026-11-22 23:59:59", MOSCOW) == datetime(
        2026, 11, 22, 20, 59, 59, tzinfo=UTC
    )


def test_an_unreadable_date_is_a_sentence_not_a_guess() -> None:
    with pytest.raises(ValueError, match="could not read the date"):
        parse_close("next sunday", MOSCOW)


def test_the_period_covers_the_whole_closing_day() -> None:
    """The close *date* decides what is counted; the close *time* only decides when.

    A `17:00` close and a `23:59:59` close must report the same week. Before this, the
    time also shaped the window, so a 17:00 close reported `15.11–22.11` - eight calendar
    days, arithmetically consistent and not what anybody meant.
    """
    for value in ("22.11.2026 17:00:00", "22.11.2026 23:59:59", "22.11.2026"):
        schedule = Schedule(period_days=7, next_close=local(value))
        period = schedule.period_for(MOSCOW)
        assert period.describe(MOSCOW) == "16.11.2026–22.11.2026 (7 дн.)", value

    # It is the whole local week, independent of the closing clock time.
    period = Schedule(period_days=7, next_close=local("22.11.2026 17:00:00")).period_for(MOSCOW)
    assert period.start == local("16.11.2026 00:00:00")
    assert period.end == local("23.11.2026 00:00:00")


def test_rolling_keeps_the_wall_clock_time() -> None:
    """Advancing by whole periods must not drift the boundary to a different hour."""
    close = local("22.11.2026 23:59:59")
    schedule = Schedule(period_days=7, next_close=close)

    rolled = schedule.rolled(after=close + timedelta(seconds=1))

    assert rolled.next_close == close + timedelta(days=7)
    assert describe_close(rolled.next_close, MOSCOW) == "29.11.2026 23:59:59"


def test_rolling_advances_past_every_missed_boundary() -> None:
    """Six weeks offline must not leave the schedule six periods behind."""
    close = local("22.11.2026 23:59:59")
    schedule = Schedule(period_days=7, next_close=close)

    rolled = schedule.rolled(after=close + timedelta(days=45))

    assert rolled.next_close > close + timedelta(days=45)
    assert rolled.next_close == close + timedelta(days=49)


def test_nothing_happens_before_the_close() -> None:
    close = local("22.11.2026 23:59:59")
    schedule = Schedule(period_days=7, next_close=close)

    due = due_at(schedule, close - timedelta(minutes=1))

    assert due.action == "wait"
    assert due.period is None


def test_a_close_is_published_right_after_it_passes() -> None:
    close = local("22.11.2026 23:59:59")
    schedule = Schedule(period_days=7, next_close=close)

    due = due_at(schedule, close + timedelta(seconds=1))

    assert due.action == "publish"
    assert due.period is not None
    assert due.period.end == close + timedelta(seconds=1)
    assert due.next_close == close + timedelta(days=7)


def test_a_close_missed_by_minutes_is_still_published() -> None:
    """A redeploy takes a minute; a restart 40 seconds late must not skip a week.

    This is the case the grace window exists for, and it is the difference between a
    useful feature and one that drops a week because the panel restarted.
    """
    close = local("22.11.2026 23:59:59")
    schedule = Schedule(period_days=7, next_close=close)

    due = due_at(schedule, close + timedelta(minutes=40))

    assert due.action == "publish"


def test_a_close_missed_by_days_is_skipped() -> None:
    """The admin asked for this: a stale week must not appear in the channel."""
    close = local("22.11.2026 23:59:59")
    schedule = Schedule(period_days=7, next_close=close)

    due = due_at(schedule, close + timedelta(days=3))

    assert due.action == "skip"
    assert due.period is not None
    assert due.next_close is not None
    assert due.next_close > close + timedelta(days=3)


def test_the_grace_window_is_a_boundary_not_a_wish() -> None:
    close = local("22.11.2026 23:59:59")
    schedule = Schedule(period_days=7, next_close=close)

    assert due_at(schedule, close + DEFAULT_GRACE).action == "publish"
    assert due_at(schedule, close + DEFAULT_GRACE + timedelta(seconds=1)).action == "skip"


def test_a_schedule_needs_a_positive_period_and_an_aware_time() -> None:
    with pytest.raises(ValueError):
        Schedule(period_days=0, next_close=datetime.now(UTC))
    with pytest.raises(ValueError):
        Schedule(period_days=7, next_close=datetime(2026, 11, 22))  # no tzinfo


def test_describe_renders_in_the_channel_timezone() -> None:
    assert describe_close(local("22.11.2026 23:59:59"), MOSCOW) == "22.11.2026 23:59:59"

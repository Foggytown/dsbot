"""Any-period counting: the inputs staff actually type.

Discord has no date picker, so dates arrive as text and the parsing has to be forgiving
about format but strict about meaning. Every case here is one a staff member will
eventually type, and each one had to be decided rather than guessed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dsbot.modules.quotas.period import (
    Period,
    parse_date,
    resolve_period,
)

MOSCOW = "Europe/Moscow"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)  # a Saturday


def test_both_date_spellings_are_accepted() -> None:
    assert parse_date("2026-11-01").isoformat() == "2026-11-01"
    assert parse_date("01.11.2026").isoformat() == "2026-11-01"
    assert parse_date("1.11.2026").isoformat() == "2026-11-01"


def test_an_unreadable_date_says_what_is_accepted() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_date("next friday")
    message = str(excinfo.value)
    assert "YYYY-MM-DD" in message and "DD.MM.YYYY" in message


def test_explicit_range_covers_both_endpoints() -> None:
    """'from 1 to 7 November' must include the 7th."""
    period = resolve_period(timezone=MOSCOW, start="01.11.2026", end="07.11.2026", now=NOW)
    assert period.label(MOSCOW) == "01.11.2026–07.11.2026"
    # 7 days inclusive, because the end date is the whole day.
    assert period.days == 7


def test_explicit_range_wins_over_a_named_week() -> None:
    period = resolve_period(
        timezone=MOSCOW, start="01.11.2026", end="01.11.2026", which="last", now=NOW
    )
    assert period.label(MOSCOW) == "01.11.2026"


def test_end_before_start_is_refused() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_period(timezone=MOSCOW, start="07.11.2026", end="01.11.2026", now=NOW)
    assert "before start" in str(excinfo.value)


def test_half_a_range_is_refused() -> None:
    """One date alone is ambiguous: start of nothing, or until forever?"""
    with pytest.raises(ValueError) as excinfo:
        resolve_period(timezone=MOSCOW, start="01.11.2026", now=NOW)
    assert "both" in str(excinfo.value)


def test_last_n_days_is_a_window_from_now() -> None:
    period = resolve_period(timezone=MOSCOW, days=30, now=NOW)
    assert period.end == NOW
    assert period.start == NOW - timedelta(days=30)
    assert period.days == 30


def test_days_wins_over_weeks() -> None:
    period = resolve_period(timezone=MOSCOW, days=5, weeks=4, now=NOW)
    assert period.days == 5


def test_zero_days_is_refused() -> None:
    with pytest.raises(ValueError):
        resolve_period(timezone=MOSCOW, days=0, now=NOW)


def test_current_week_is_the_default() -> None:
    period = resolve_period(timezone=MOSCOW, now=NOW)
    assert period.label(MOSCOW) == "16.11.2026–22.11.2026"


def test_last_week() -> None:
    period = resolve_period(timezone=MOSCOW, which="last", now=NOW)
    assert period.label(MOSCOW) == "09.11.2026–15.11.2026"


def test_weeks_back_still_works() -> None:
    period = resolve_period(timezone=MOSCOW, weeks=3, now=NOW)
    assert period.end == resolve_period(timezone=MOSCOW, now=NOW).end
    assert period.days == 21


def test_describe_shows_how_long_the_window_is() -> None:
    """A hand-picked range must not be mistaken for a standard week."""
    period: Period = resolve_period(timezone=MOSCOW, start="01.11.2026", end="07.11.2026", now=NOW)
    assert period.describe(MOSCOW) == "01.11.2026–07.11.2026 (7 дн.)"


def test_a_one_day_range_is_labelled_with_one_date() -> None:
    period = resolve_period(timezone=MOSCOW, start="05.11.2026", end="05.11.2026", now=NOW)
    assert period.label(MOSCOW) == "05.11.2026"


def test_a_year_long_range_is_allowed() -> None:
    """No arbitrary cap: 'everything this year' is a legitimate question."""
    period = resolve_period(timezone=MOSCOW, start="01.01.2026", end="31.12.2026", now=NOW)
    assert period.days == 365

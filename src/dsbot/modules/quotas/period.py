"""Reporting periods.

A period is a half-open interval `[start, end)` in one timezone. Half-open matters: a
report posted exactly at the boundary must belong to exactly one week, and `<=` on both
ends would double-count it while `<` on both would lose it.

Weeks are ISO weeks: Monday 00:00 to the next Monday 00:00. The timezone is explicit
because "last week" is a civil fact, not a UTC one: a community in Moscow expects the
week to end at their midnight, not three hours later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE: Final = "Europe/Moscow"


@dataclass(frozen=True, slots=True)
class Period:
    """One counting window, always stored as aware UTC datetimes."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"period end {self.end} must be after start {self.start}")

    @property
    def days(self) -> int:
        return max(1, round((self.end - self.start).total_seconds() / 86400))

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def label(self, timezone: str = DEFAULT_TIMEZONE) -> str:
        zone = load_timezone(timezone)
        start = self.start.astimezone(zone)
        end = (self.end - timedelta(seconds=1)).astimezone(zone)
        if start.date() == end.date():
            return f"{start:%d.%m.%Y}"
        return f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"


def load_timezone(name: str | None) -> ZoneInfo:
    """Resolve a timezone, refusing to silently fall back to UTC.

    A wrong timezone shifts every boundary by hours and is invisible in the output, so
    an unknown name is an error rather than a default.
    """
    candidate = (name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone {candidate!r}") from exc


def week_of(moment: datetime, timezone: str = DEFAULT_TIMEZONE) -> Period:
    """The ISO week containing `moment`, as UTC instants."""
    zone = load_timezone(timezone)
    local = moment.astimezone(zone)
    monday = local.date() - timedelta(days=local.weekday())
    return _local_week(monday, zone)


def current_week(now: datetime | None = None, timezone: str = DEFAULT_TIMEZONE) -> Period:
    return week_of(now or datetime.now(UTC), timezone)


def previous_week(now: datetime | None = None, timezone: str = DEFAULT_TIMEZONE) -> Period:
    current = current_week(now, timezone)
    zone = load_timezone(timezone)
    monday = current.start.astimezone(zone).date() - timedelta(days=7)
    return _local_week(monday, zone)


def weeks_back(count: int, now: datetime | None = None, timezone: str = DEFAULT_TIMEZONE) -> Period:
    """A window of `count` consecutive ISO weeks ending with the current one."""
    if count < 1:
        raise ValueError("count must be at least 1")
    current = current_week(now, timezone)
    zone = load_timezone(timezone)
    monday = current.start.astimezone(zone).date() - timedelta(weeks=count - 1)
    period = _local_week(monday, zone)
    return Period(start=period.start, end=current.end)


def explicit_period(
    start: date | datetime, end: date | datetime, timezone: str = DEFAULT_TIMEZONE
) -> Period:
    """Build a period from calendar dates.

    A bare date means "the whole day": `start` becomes its 00:00 and `end` the next
    midnight, so `--from 2026-11-10 --to 2026-11-17` covers ten through sixteen and
    excludes the seventeenth, which is what an admin writing a week by hand expects.
    """
    zone = load_timezone(timezone)
    start_at = _start_of(start, zone)
    end_at = _start_of(end, zone)
    if isinstance(end, date) and not isinstance(end, datetime):
        end_at += timedelta(days=1)
    return Period(start=start_at.astimezone(UTC), end=end_at.astimezone(UTC))


def _start_of(value: date | datetime, zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=zone)
        return aware.astimezone(zone)
    return datetime.combine(value, time.min, tzinfo=zone)


def _local_week(monday: date, zone: ZoneInfo) -> Period:
    start = datetime.combine(monday, time.min, tzinfo=zone)
    return Period(
        start=start.astimezone(UTC),
        end=(start + timedelta(days=7)).astimezone(UTC),
    )


__all__ = [
    "DEFAULT_TIMEZONE",
    "Period",
    "current_week",
    "explicit_period",
    "load_timezone",
    "previous_week",
    "week_of",
    "weeks_back",
]

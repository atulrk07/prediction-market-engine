"""Validate a bounded, timezone-aware history window."""

from datetime import datetime, timedelta, timezone


DEFAULT_HISTORY_HOURS = 24
MAX_HISTORY_DAYS = 7


def history_range(start: datetime | None, end: datetime | None, now: datetime) -> tuple[datetime, datetime]:
    for value in (start, end, now):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("History timestamps must include a timezone")
    end = (end if end is not None else now).astimezone(timezone.utc)
    try:
        start = start.astimezone(timezone.utc) if start is not None else end - timedelta(hours=DEFAULT_HISTORY_HOURS)
    except OverflowError:
        raise ValueError("History start is outside the supported datetime range") from None
    if start >= end:
        raise ValueError("History start must be before end")
    if end - start > timedelta(days=MAX_HISTORY_DAYS):
        raise ValueError("History range cannot exceed 7 days")
    return start, end

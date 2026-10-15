"""Synthetic time windows; no database or exchange access needed."""

from datetime import datetime, timedelta, timezone

import pytest

from history import history_range


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def test_default_is_last_24_hours():
    assert history_range(None, None, NOW) == (NOW - timedelta(days=1), NOW)


def test_only_end_defaults_start_relative_to_end():
    end = NOW - timedelta(days=2)
    assert history_range(None, end, NOW) == (end - timedelta(days=1), end)


def test_only_start_defaults_end_to_now():
    start = NOW - timedelta(hours=2)
    assert history_range(start, None, NOW) == (start, NOW)


def test_range_accepts_exactly_seven_days_and_normalizes_offsets():
    local_zone = timezone(timedelta(hours=-7))
    start = (NOW - timedelta(days=7)).astimezone(local_zone)
    end = NOW.astimezone(local_zone)
    assert history_range(start, end, NOW) == (NOW - timedelta(days=7), NOW)


@pytest.mark.parametrize("start, end", [
    (NOW, NOW), (NOW, NOW - timedelta(seconds=1)),
    (NOW - timedelta(days=7, microseconds=1), NOW),
])
def test_invalid_ranges_fail(start, end):
    with pytest.raises(ValueError):
        history_range(start, end, NOW)


@pytest.mark.parametrize("position", ["start", "end", "now"])
def test_all_supplied_times_must_be_aware(position):
    values = {"start": NOW - timedelta(hours=1), "end": NOW, "now": NOW}
    values[position] = values[position].replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone"):
        history_range(**values)


def test_default_window_cannot_underflow_supported_dates():
    with pytest.raises(ValueError, match="supported datetime range"):
        history_range(None, datetime.min.replace(tzinfo=timezone.utc), NOW)

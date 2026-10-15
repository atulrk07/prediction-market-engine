"""Controlled-clock checks; these values are synthetic, never live data."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from latest_quote import build_latest_quote, stale_after_seconds


OBSERVED_AT = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
MARKET = {"id": 1, "external_id": "FIXTURE-FRESHNESS", "status": "active"}
SNAPSHOT = {
    "id": 2, "ingestion_run_id": 3, "collection_slot": OBSERVED_AT,
    "observed_at": OBSERVED_AT, "source_timestamp": OBSERVED_AT - timedelta(days=1),
    "yes_bid": Decimal("0.4201"), "yes_bid_quantity": Decimal("10.25"),
    "yes_ask": Decimal("0.4498"), "yes_ask_quantity": Decimal("8.75"),
}


@pytest.mark.parametrize("age, stale", [(0, False), (119.9, False), (120, False), (120.1, True), (180, True), (-1, True)])
def test_age_uses_collection_time_and_preserves_values(age, stale):
    result = build_latest_quote(MARKET, SNAPSHOT, OBSERVED_AT + timedelta(seconds=age), 120)
    assert result["age_seconds"] == age
    assert result["is_stale"] is stale
    assert result["status"] == "two_sided"
    assert result["quote"].yes_bid == Decimal("0.4201")
    assert result["quote"].source_timestamp == SNAPSHOT["source_timestamp"]


def test_age_is_consistent_across_timezone_offsets():
    local_now = (OBSERVED_AT + timedelta(seconds=180)).astimezone(timezone(timedelta(hours=-7)))
    assert build_latest_quote(MARKET, SNAPSHOT, local_now, 120)["age_seconds"] == 180
    with pytest.raises(ValueError, match="timezone-aware"):
        build_latest_quote(MARKET, SNAPSHOT, local_now.replace(tzinfo=None), 120)


def test_default_threshold(monkeypatch):
    monkeypatch.delenv("STALE_AFTER_SECONDS", raising=False)
    assert stale_after_seconds() == 120


@pytest.mark.parametrize("raw", ["0", "-1", "wrong", "1.5", ""])
def test_invalid_threshold_fails_clearly(monkeypatch, raw):
    monkeypatch.setenv("STALE_AFTER_SECONDS", raw)
    with pytest.raises(ValueError, match="positive integer"):
        stale_after_seconds()

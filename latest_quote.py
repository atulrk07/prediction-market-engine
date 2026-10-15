"""Describe the latest stored observation and its collection age."""

from datetime import datetime, timezone
import os

from persistence import quote_from_snapshot


DEFAULT_STALE_AFTER_SECONDS = 120


def stale_after_seconds() -> int:
    """Read a positive integer threshold once when the API starts."""
    raw = os.environ.get("STALE_AFTER_SECONDS", str(DEFAULT_STALE_AFTER_SECONDS))
    try:
        seconds = int(raw)
    except ValueError:
        raise ValueError("STALE_AFTER_SECONDS must be a positive integer") from None
    if seconds <= 0:
        raise ValueError("STALE_AFTER_SECONDS must be a positive integer")
    return seconds


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_latest_quote(market: dict, snapshot: dict | None, now: datetime, threshold: int) -> dict:
    """Keep liquidity status separate from age and stored exchange market status."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Current time must be timezone-aware")
    result = {
        "market_id": market["id"], "ticker": market["external_id"],
        "market_status": market["status"], "status": "no_observations",
        "snapshot_id": None, "ingestion_run_id": None, "collection_slot": None,
        "age_seconds": None, "is_stale": None, "stale_after_seconds": threshold,
        "quote": None,
    }
    if snapshot is None:
        return result

    quote = quote_from_snapshot(snapshot, market["external_id"])
    if quote.is_crossed:
        status = "crossed"
    elif quote.yes_bid is None and quote.yes_ask is None:
        status = "empty_book"
    elif quote.yes_bid is None or quote.yes_ask is None:
        status = "one_sided"
    else:
        status = "two_sided"
    age = (now - quote.observed_at).total_seconds()
    result.update(
        status=status, snapshot_id=snapshot["id"],
        ingestion_run_id=snapshot["ingestion_run_id"], collection_slot=snapshot["collection_slot"],
        age_seconds=age,
        # A future timestamp signals clock skew; do not claim its age is zero.
        is_stale=age < 0 or age > threshold,
        quote=quote,
    )
    return result

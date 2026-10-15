"""Save/replay one observation transactionally and read it by snapshot ID."""

from datetime import datetime, timezone
from collections.abc import Mapping
import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from models import ingestion_runs, markets, snapshots
from normalize_quote import Quote
from run_records import put_outcome


def minute_slot(at: datetime) -> datetime:
    """The one-shot command uses a 60-second UTC collection slot."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Collection time must be timezone-aware")
    return at.astimezone(timezone.utc).replace(second=0, microsecond=0)


def market_values(market: dict, ticker: str) -> dict:
    """Validate metadata and map exchange fields to our database columns."""
    for field in ("ticker", "title", "status"):
        if not isinstance(market.get(field), str) or not market[field].strip():
            raise ValueError(f"Market has missing or invalid {field}")
    if market["ticker"] != ticker or market.get("market_type") != "binary":
        raise ValueError("Metadata must describe the quote's binary market")
    raw_close = market.get("close_time")
    close_time = None
    if raw_close is not None:
        if not isinstance(raw_close, str):
            raise ValueError("close_time must be an ISO timestamp string or null")
        close_time = datetime.fromisoformat(raw_close)
        if close_time.tzinfo is None or close_time.utcoffset() is None:
            raise ValueError("close_time must be timezone-aware")
        close_time = close_time.astimezone(timezone.utc)
    return dict(platform="kalshi", external_id=ticker, title=market["title"],
                status=market["status"], close_time=close_time)


def _upsert_market(connection, values: dict) -> int:
    statement = insert(markets).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_markets_platform_external_id",
        set_={"title": statement.excluded.title, "status": statement.excluded.status,
              "close_time": statement.excluded.close_time, "updated_at": func.now()},
    ).returning(markets.c.id)
    return connection.execute(statement).scalar_one()


def save_market_metadata(engine: Engine, market: dict, ticker: str) -> int:
    """Refresh non-active market metadata without deleting or creating snapshots."""
    values = market_values(market, ticker)
    with engine.begin() as connection:
        return _upsert_market(connection, values)


def _save_snapshot(connection, run_id, values, quote, collection_slot) -> dict:
    market_id = _upsert_market(connection, values)
    statement = insert(snapshots).values(
        market_id=market_id, ingestion_run_id=run_id, collection_slot=collection_slot,
        observed_at=quote.observed_at, source_timestamp=quote.source_timestamp,
        yes_bid=quote.yes_bid, yes_bid_quantity=quote.yes_bid_quantity,
        yes_ask=quote.yes_ask, yes_ask_quantity=quote.yes_ask_quantity,
    ).on_conflict_do_nothing(constraint="uq_snapshots_market_slot").returning(snapshots.c.id)
    snapshot_id = connection.execute(statement).scalar_one_or_none()
    outcome = "inserted" if snapshot_id is not None else "duplicate"
    if snapshot_id is None:
        snapshot_id = connection.execute(select(snapshots.c.id).where(
            snapshots.c.market_id == market_id, snapshots.c.collection_slot == collection_slot,
        )).scalar_one()
    return {"snapshot_id": snapshot_id, "run_id": run_id, "outcome": outcome}


def save_in_run(engine, run_id, market, quote, collection_slot, info) -> dict:
    """Commit this market/snapshot and its cycle outcome together."""
    values = market_values(market, quote.ticker)
    with engine.begin() as connection:
        run = connection.execute(select(ingestion_runs).where(ingestion_runs.c.id == run_id).with_for_update()).mappings().one()
        if run["collection_slot"] != collection_slot or run["finished_at"] is not None:
            raise ValueError("Snapshot must belong to its unfinished run's slot")
        saved = _save_snapshot(connection, run_id, values, quote, collection_slot)
        put_outcome(connection, run_id, quote.ticker, {**info, "attempted": True, **saved})
    return saved


def save_inactive_in_run(engine, run_id, market, ticker, info):
    values = market_values(market, ticker)
    with engine.begin() as connection:
        _upsert_market(connection, values)
        put_outcome(connection, run_id, ticker, {**info, "outcome": "not_attempted", "attempted": True,
                                              "reason": "market_not_active", "market_status": market["status"]})


def save_observation(engine: Engine, market: dict, quote: Quote, collection_slot: datetime, *, interval_seconds: int = 60) -> dict:
    """Save the first market/slot observation; return its ID and this attempt's outcome."""
    values = market_values(market, quote.ticker)
    if type(interval_seconds) is not int or interval_seconds <= 0:
        raise ValueError("interval_seconds must be a positive integer")
    if collection_slot.tzinfo is None or collection_slot.utcoffset() is None:
        raise ValueError("collection_slot must be timezone-aware")
    collection_slot = collection_slot.astimezone(timezone.utc)

    # Keep a started-run record even if the following write is interrupted.
    with engine.begin() as connection:
        run_id = connection.execute(ingestion_runs.insert().values(
            collection_slot=collection_slot,
            attempted_count=1,
            watchlist_config={"watchlist": [quote.ticker], "interval_seconds": interval_seconds},
        ).returning(ingestion_runs.c.id)).scalar_one()
    logging.info("Run %s: saving %s", run_id, quote.ticker)

    with engine.begin() as connection:
        saved = _save_snapshot(connection, run_id, values, quote, collection_slot)
        inserted_count = connection.execute(select(func.count()).select_from(snapshots).where(
            snapshots.c.ingestion_run_id == run_id,
        )).scalar_one()
        connection.execute(ingestion_runs.update().where(ingestion_runs.c.id == run_id).values(
            status="success", finished_at=func.now(), inserted_count=inserted_count,
            duplicate_count=1 if saved["outcome"] == "duplicate" else 0,
            market_outcomes={quote.ticker: {"outcome": saved["outcome"], "snapshot_id": saved["snapshot_id"]}},
        ))
    return saved


def quote_from_snapshot(snapshot: Mapping, ticker: str) -> Quote:
    """Rebuild a normalized quote from exact stored prices without any HTTP."""
    bid, ask = snapshot["yes_bid"], snapshot["yes_ask"]
    spread = ask - bid if ask is not None and bid is not None else None
    return Quote(
        ticker=ticker, observed_at=snapshot["observed_at"], source_timestamp=snapshot["source_timestamp"],
        yes_bid=bid, yes_bid_quantity=snapshot["yes_bid_quantity"],
        yes_ask=ask, yes_ask_quantity=snapshot["yes_ask_quantity"],
        spread=spread, is_crossed=spread is not None and spread < 0,
    )


def read_observation(engine: Engine, snapshot_id: int) -> dict | None:
    """Read one stored snapshot; this function never calls the exchange."""
    statement = select(
        snapshots, markets.c.external_id.label("ticker"), markets.c.title,
        markets.c.status.label("market_status"), markets.c.close_time,
    ).join(markets, snapshots.c.market_id == markets.c.id).where(snapshots.c.id == snapshot_id)
    with engine.connect() as connection:
        row = connection.execute(statement).mappings().one_or_none()
    if row is None:
        return None
    quote = quote_from_snapshot(row, row["ticker"])
    return {
        "snapshot_id": row["id"], "market_id": row["market_id"],
        "ingestion_run_id": row["ingestion_run_id"],
        "collection_slot": row["collection_slot"].isoformat(),
        "title": row["title"], "market_status": row["market_status"],
        "close_time": row["close_time"].isoformat() if row["close_time"] is not None else None,
        "quote": quote.model_dump(mode="json"),
    }

"""Save/replay one observation transactionally and read it by snapshot ID."""

from datetime import datetime, timezone
import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from models import ingestion_runs, markets, snapshots
from normalize_quote import Quote


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


def save_observation(engine: Engine, market: dict, quote: Quote, collection_slot: datetime) -> dict:
    """Save the first market/slot observation; return its ID and this attempt's outcome."""
    values = market_values(market, quote.ticker)
    if collection_slot.tzinfo is None or collection_slot.utcoffset() is None:
        raise ValueError("collection_slot must be timezone-aware")
    collection_slot = collection_slot.astimezone(timezone.utc)

    # Keep a started-run record even if the following write is interrupted.
    with engine.begin() as connection:
        run_id = connection.execute(ingestion_runs.insert().values(
            collection_slot=collection_slot,
            attempted_count=1,
            watchlist_config={"watchlist": [quote.ticker], "interval_seconds": 60},
        ).returning(ingestion_runs.c.id)).scalar_one()
    logging.info("Run %s: saving %s", run_id, quote.ticker)

    with engine.begin() as connection:
        statement = insert(markets).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="uq_markets_platform_external_id",
            set_={"title": statement.excluded.title, "status": statement.excluded.status,
                  "close_time": statement.excluded.close_time, "updated_at": func.now()},
        ).returning(markets.c.id)
        market_id = connection.execute(statement).scalar_one()
        snapshot_statement = insert(snapshots).values(
            market_id=market_id, ingestion_run_id=run_id, collection_slot=collection_slot,
            observed_at=quote.observed_at, source_timestamp=quote.source_timestamp,
            yes_bid=quote.yes_bid, yes_bid_quantity=quote.yes_bid_quantity,
            yes_ask=quote.yes_ask, yes_ask_quantity=quote.yes_ask_quantity,
        ).on_conflict_do_nothing(
            constraint="uq_snapshots_market_slot",
        ).returning(snapshots.c.id)
        snapshot_id = connection.execute(snapshot_statement).scalar_one_or_none()
        outcome = "inserted" if snapshot_id is not None else "duplicate"
        if snapshot_id is None:
            # Read only after PostgreSQL has resolved the insert conflict.
            snapshot_id = connection.execute(select(snapshots.c.id).where(
                snapshots.c.market_id == market_id,
                snapshots.c.collection_slot == collection_slot,
            )).scalar_one()
        inserted_count = connection.execute(select(func.count()).select_from(snapshots).where(
            snapshots.c.ingestion_run_id == run_id,
        )).scalar_one()
        connection.execute(ingestion_runs.update().where(ingestion_runs.c.id == run_id).values(
            status="success", finished_at=func.now(), inserted_count=inserted_count,
            duplicate_count=1 if outcome == "duplicate" else 0,
            market_outcomes={quote.ticker: {"outcome": outcome, "snapshot_id": snapshot_id}},
        ))
    return {"snapshot_id": snapshot_id, "run_id": run_id, "outcome": outcome}


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
    bid, ask = row["yes_bid"], row["yes_ask"]
    spread = ask - bid if ask is not None and bid is not None else None
    quote = Quote(
        ticker=row["ticker"], observed_at=row["observed_at"], source_timestamp=row["source_timestamp"],
        yes_bid=bid, yes_bid_quantity=row["yes_bid_quantity"],
        yes_ask=ask, yes_ask_quantity=row["yes_ask_quantity"],
        spread=spread, is_crossed=spread is not None and spread < 0,
    )
    return {
        "snapshot_id": row["id"], "market_id": row["market_id"],
        "ingestion_run_id": row["ingestion_run_id"],
        "collection_slot": row["collection_slot"].isoformat(),
        "title": row["title"], "market_status": row["market_status"],
        "close_time": row["close_time"].isoformat() if row["close_time"] is not None else None,
        "quote": quote.model_dump(mode="json"),
    }

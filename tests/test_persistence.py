"""Synthetic save/read checks on isolated PostgreSQL; all created rows cleaned up."""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert

from database import make_engine
from models import ingestion_runs, markets, snapshots
from normalize_quote import normalize_orderbook
from persistence import minute_slot, read_observation, save_observation
from replay_quote import replay_observation


BOOKS = json.loads((Path(__file__).parent / "fixtures" / "orderbooks.json").read_text())
OBSERVED_AT = datetime(2026, 10, 13, 12, 0, 14, 123456, tzinfo=timezone.utc)


@pytest.fixture
def sample_factory(postgres_engine):
    tickers = []

    def make_sample():
        ticker = f"FIXTURE-{uuid4()}"
        tickers.append(ticker)
        market = {"ticker": ticker, "title": "Synthetic persistence test", "status": "active",
                  "market_type": "binary", "close_time": "2026-10-14T05:00:00Z"}
        quote = normalize_orderbook(BOOKS["subpenny"], ticker, OBSERVED_AT)
        return market, quote

    yield make_sample
    # Only delete this test's UUID-tagged rows, in foreign-key order.
    for ticker in tickers:
        with postgres_engine.begin() as connection:
            ids = select(markets.c.id).where(markets.c.platform == "kalshi", markets.c.external_id == ticker)
            connection.execute(snapshots.delete().where(snapshots.c.market_id.in_(ids)))
            connection.execute(ingestion_runs.delete().where(
                ingestion_runs.c.watchlist_config.contains({"watchlist": [ticker]}),
            ))
            connection.execute(markets.delete().where(markets.c.platform == "kalshi", markets.c.external_id == ticker))


@pytest.fixture
def sample(sample_factory):
    return sample_factory()


@pytest.mark.postgres
def test_committed_observation_reads_back_through_a_new_engine(postgres_engine, sample):
    market, quote = sample
    slot = minute_slot(quote.observed_at)
    snapshot_id = save_observation(postgres_engine, market, quote, slot)["snapshot_id"]
    fresh_engine = make_engine(postgres_engine.url.render_as_string(hide_password=False))
    try:
        result = read_observation(fresh_engine, snapshot_id)
    finally:
        fresh_engine.dispose()
    assert result["snapshot_id"] == snapshot_id
    assert result["title"] == market["title"]
    assert result["close_time"] == "2026-10-14T05:00:00+00:00"
    assert result["collection_slot"] == "2026-10-13T12:00:00+00:00"
    assert result["quote"] == quote.model_dump(mode="json")
    assert result["quote"]["yes_bid"] == "0.4201"
    assert result["quote"]["yes_ask"] == "0.4498"
    with postgres_engine.connect() as connection:
        run = connection.execute(select(ingestion_runs).where(
            ingestion_runs.c.id == result["ingestion_run_id"],
        )).mappings().one()
        assert run["status"] == "success" and run["finished_at"] is not None
        assert run["attempted_count"] == run["inserted_count"] == 1
        count = connection.execute(select(func.count()).select_from(snapshots).where(
            snapshots.c.ingestion_run_id == run["id"],
        )).scalar_one()
        assert run["inserted_count"] == count
        assert run["market_outcomes"][quote.ticker] == {"outcome": "inserted", "snapshot_id": snapshot_id}


@pytest.mark.postgres
def test_metadata_update_reuses_the_market_row(postgres_engine, sample):
    market, quote = sample
    first = read_observation(postgres_engine, save_observation(postgres_engine, market, quote, minute_slot(quote.observed_at))["snapshot_id"])
    next_quote = quote.model_copy(update={"observed_at": quote.observed_at + timedelta(minutes=1),
                                        "yes_bid": Decimal("0.4301"), "spread": Decimal("0.0197")})
    changed_market = dict(market, title="Updated synthetic title", status="closed")
    second = read_observation(postgres_engine, save_observation(postgres_engine, changed_market, next_quote, minute_slot(next_quote.observed_at))["snapshot_id"])
    assert first["market_id"] == second["market_id"]
    assert first["snapshot_id"] != second["snapshot_id"]
    assert second["title"] == "Updated synthetic title"
    assert second["market_status"] == "closed"
    assert read_observation(postgres_engine, first["snapshot_id"])["quote"] == quote.model_dump(mode="json")


@pytest.mark.postgres
@pytest.mark.parametrize("case", ["empty", "no_only"])
def test_missing_prices_survive_database_storage(postgres_engine, sample, case):
    market, original = sample
    quote = normalize_orderbook(BOOKS[case], original.ticker, OBSERVED_AT)
    snapshot_id = save_observation(postgres_engine, market, quote, minute_slot(OBSERVED_AT))["snapshot_id"]
    assert read_observation(postgres_engine, snapshot_id)["quote"] == quote.model_dump(mode="json")


@pytest.mark.postgres
def test_failed_snapshot_rolls_back_the_metadata_update(postgres_engine, sample):
    market, quote = sample
    snapshot_id = save_observation(postgres_engine, market, quote, minute_slot(OBSERVED_AT))["snapshot_id"]
    # Intentionally bypass Pydantic validation to make PostgreSQL reject a write.
    bad_quote = quote.model_copy(update={"yes_bid": Decimal("1.1"),
                                        "observed_at": OBSERVED_AT + timedelta(minutes=1)})
    with pytest.raises(IntegrityError):
        save_observation(postgres_engine, dict(market, title="Must roll back"), bad_quote, minute_slot(bad_quote.observed_at))
    assert read_observation(postgres_engine, snapshot_id)["title"] == market["title"]
    with postgres_engine.connect() as connection:
        unfinished = connection.execute(select(ingestion_runs).where(
            ingestion_runs.c.watchlist_config.contains({"watchlist": [quote.ticker]}),
            ingestion_runs.c.finished_at.is_(None),
        )).mappings().one()
        assert unfinished["status"] == "running"
        assert unfinished["inserted_count"] == 0
        count = connection.execute(select(func.count()).select_from(snapshots).where(
            snapshots.c.ingestion_run_id == unfinished["id"],
        )).scalar_one()
        assert count == 0


@pytest.mark.postgres
def test_invalid_metadata_does_not_create_a_run(postgres_engine, sample):
    market, quote = sample
    with pytest.raises(ValueError, match="title"):
        save_observation(postgres_engine, dict(market, title=None), quote, minute_slot(OBSERVED_AT))
    with postgres_engine.connect() as connection:
        count = connection.execute(select(func.count()).select_from(ingestion_runs).where(
            ingestion_runs.c.watchlist_config.contains({"watchlist": [quote.ticker]}),
        )).scalar_one()
        assert count == 0


def test_minute_slot_preserves_timezone_and_removes_seconds():
    local_time = OBSERVED_AT.astimezone(timezone(timedelta(hours=-7)))
    assert minute_slot(local_time) == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone-aware"):
        minute_slot(local_time.replace(tzinfo=None))


@pytest.mark.postgres
def test_repeated_slot_preserves_every_snapshot_field_and_run_owner(postgres_engine, sample):
    market, quote = sample
    slot = minute_slot(OBSERVED_AT)
    first = save_observation(postgres_engine, market, quote, slot)
    with postgres_engine.connect() as connection:
        original = dict(connection.execute(select(snapshots).where(
            snapshots.c.id == first["snapshot_id"],
        )).mappings().one())
    changed = quote.model_copy(update={"observed_at": OBSERVED_AT + timedelta(seconds=7),
                                       "yes_bid": Decimal("0.4301"), "spread": Decimal("0.0197")})
    repeated = save_observation(postgres_engine, market, changed, slot)
    assert first["outcome"] == "inserted" and repeated["outcome"] == "duplicate"
    assert repeated["snapshot_id"] == first["snapshot_id"]
    assert repeated["run_id"] != first["run_id"]
    with postgres_engine.connect() as connection:
        after = dict(connection.execute(select(snapshots).where(
            snapshots.c.id == first["snapshot_id"],
        )).mappings().one())
        assert after == original
        assert after["ingestion_run_id"] == first["run_id"]
        run = connection.execute(select(ingestion_runs).where(
            ingestion_runs.c.id == repeated["run_id"],
        )).mappings().one()
        assert run["status"] == "success"
        assert run["inserted_count"] == 0 and run["duplicate_count"] == 1
        assert run["market_outcomes"][quote.ticker] == {"outcome": "duplicate", "snapshot_id": first["snapshot_id"]}


@pytest.mark.postgres
def test_same_price_in_later_slot_is_new_history(postgres_engine, sample):
    market, quote = sample
    first = save_observation(postgres_engine, market, quote, minute_slot(OBSERVED_AT))
    later_quote = quote.model_copy(update={"observed_at": OBSERVED_AT + timedelta(minutes=1)})
    later = save_observation(postgres_engine, market, later_quote, minute_slot(later_quote.observed_at))
    assert first["outcome"] == later["outcome"] == "inserted"
    assert first["snapshot_id"] != later["snapshot_id"]
    original = read_observation(postgres_engine, first["snapshot_id"])
    new = read_observation(postgres_engine, later["snapshot_id"])
    assert original["quote"]["yes_bid"] == new["quote"]["yes_bid"]
    assert original["collection_slot"] != new["collection_slot"]


@pytest.mark.postgres
def test_competing_full_writes_keep_one_snapshot(postgres_engine, sample):
    market, quote = sample
    other = quote.model_copy(update={"yes_bid": Decimal("0.4301"), "spread": Decimal("0.0197"),
                                     "observed_at": OBSERVED_AT + timedelta(seconds=2)})
    gate = Barrier(2, timeout=5)
    url = postgres_engine.url.render_as_string(hide_password=False)

    def write(candidate):
        engine = make_engine(url)  # Independent client/connection for each writer.
        try:
            gate.wait()
            return save_observation(engine, market, candidate, minute_slot(OBSERVED_AT))
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as workers:
        tasks = [workers.submit(write, candidate) for candidate in (quote, other)]
        results = [task.result(timeout=15) for task in tasks]
    assert sorted(result["outcome"] for result in results) == ["duplicate", "inserted"]
    assert results[0]["snapshot_id"] == results[1]["snapshot_id"]
    winner = 0 if results[0]["outcome"] == "inserted" else 1
    stored = read_observation(postgres_engine, results[winner]["snapshot_id"])
    assert stored["quote"] == (quote, other)[winner].model_dump(mode="json")
    assert stored["ingestion_run_id"] == results[winner]["run_id"]
    with postgres_engine.connect() as connection:
        count = connection.execute(select(func.count()).select_from(snapshots).where(
            snapshots.c.market_id == stored["market_id"],
        )).scalar_one()
        assert count == 1


@pytest.mark.postgres
def test_competing_snapshot_inserts_are_guarded_without_metadata_upserts(postgres_engine, sample):
    market, quote = sample
    first = save_observation(postgres_engine, market, quote, minute_slot(OBSERVED_AT))
    market_id = read_observation(postgres_engine, first["snapshot_id"])["market_id"]
    slot = minute_slot(OBSERVED_AT) + timedelta(minutes=1)
    with postgres_engine.begin() as connection:
        run_ids = [connection.execute(ingestion_runs.insert().values(
            collection_slot=slot, watchlist_config={"watchlist": [quote.ticker]},
        ).returning(ingestion_runs.c.id)).scalar_one() for _ in range(2)]
    gate = Barrier(2, timeout=5)

    def raw_write(run_id):
        with postgres_engine.begin() as connection:
            gate.wait()
            return connection.execute(insert(snapshots).values(
                market_id=market_id, ingestion_run_id=run_id, collection_slot=slot,
                observed_at=OBSERVED_AT + timedelta(minutes=1),
                yes_bid=quote.yes_bid, yes_bid_quantity=quote.yes_bid_quantity,
                yes_ask=quote.yes_ask, yes_ask_quantity=quote.yes_ask_quantity,
            ).on_conflict_do_nothing(constraint="uq_snapshots_market_slot").returning(
                snapshots.c.id,
            )).scalar_one_or_none()

    with ThreadPoolExecutor(max_workers=2) as workers:
        tasks = [workers.submit(raw_write, run_id) for run_id in run_ids]
        results = [task.result(timeout=15) for task in tasks]
    assert sum(result is not None for result in results) == 1
    with postgres_engine.connect() as connection:
        count = connection.execute(select(func.count()).select_from(snapshots).where(
            snapshots.c.market_id == market_id, snapshots.c.collection_slot == slot,
        )).scalar_one()
        assert count == 1


@pytest.mark.postgres
def test_failed_market_does_not_undo_or_block_another_market(postgres_engine, sample_factory):
    market_a, quote_a = sample_factory()
    market_b, quote_b = sample_factory()
    first = save_observation(postgres_engine, market_a, quote_a, minute_slot(OBSERVED_AT))
    bad = quote_a.model_copy(update={"yes_ask_quantity": Decimal("-1"),
                                    "observed_at": OBSERVED_AT + timedelta(minutes=1)})
    with pytest.raises(IntegrityError):
        save_observation(postgres_engine, dict(market_a, title="Must roll back"), bad, minute_slot(bad.observed_at))
    second = save_observation(postgres_engine, market_b, quote_b, minute_slot(OBSERVED_AT))
    assert second["outcome"] == "inserted"
    assert read_observation(postgres_engine, first["snapshot_id"])["title"] == market_a["title"]
    assert read_observation(postgres_engine, second["snapshot_id"])["quote"] == quote_b.model_dump(mode="json")


@pytest.mark.postgres
def test_replay_keeps_original_slot_and_collection_time_without_http(postgres_engine, sample):
    market, quote = sample
    first = save_observation(postgres_engine, market, quote, minute_slot(OBSERVED_AT))
    replayed = replay_observation(postgres_engine, first["snapshot_id"])
    assert replayed["outcome"] == "duplicate"
    assert replayed["snapshot_id"] == first["snapshot_id"]
    assert replayed["observation"]["collection_slot"] == minute_slot(OBSERVED_AT).isoformat()
    assert replayed["observation"]["quote"] == quote.model_dump(mode="json")

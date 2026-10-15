"""Synthetic cycle outcomes and interruption recovery on real PostgreSQL."""

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from fake_time import FakeTime
from fetch_raw import BASE_URL, TIMEOUT
from models import ingestion_runs, markets, snapshots
from normalize_quote import normalize_orderbook
from persistence import save_in_run
from run_records import finish_run, read_run, record_outcome, start_run
import worker


BASE = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
BOOK = json.loads((Path(__file__).parent / "fixtures" / "orderbooks.json").read_text())["two_sided"]


def metadata(ticker):
    return {"ticker": ticker, "title": "Synthetic cycle audit", "market_type": "binary", "status": "active"}


@pytest.fixture
def cycle_tickers(postgres_engine):
    tickers = [f"FIXTURE-CYCLE-{uuid4()}" for _ in range(2)]
    yield tickers
    with postgres_engine.begin() as connection:
        ids = select(markets.c.id).where(markets.c.external_id.in_(tickers))
        connection.execute(snapshots.delete().where(snapshots.c.market_id.in_(ids)))
        for ticker in tickers:
            connection.execute(ingestion_runs.delete().where(ingestion_runs.c.watchlist_config.contains({"watchlist": [ticker]})))
        connection.execute(markets.delete().where(markets.c.external_id.in_(tickers)))


@pytest.mark.postgres
def test_unfinished_run_reports_committed_rows_without_inventing_success(postgres_engine, cycle_tickers):
    run_id = start_run(postgres_engine, BASE, cycle_tickers, {"interval_seconds": 60})
    first, second = cycle_tickers
    record_outcome(postgres_engine, run_id, first, {"outcome": "unknown", "attempted": True})
    saved = save_in_run(postgres_engine, run_id, metadata(first), normalize_orderbook(BOOK, first, BASE), BASE, {})
    record_outcome(postgres_engine, run_id, second, {"outcome": "unknown", "attempted": True})
    # Simulate a process ending after the first commit but before cycle finalization.
    report = read_run(postgres_engine, run_id)
    assert report["status"] == "incomplete" and report["stored_status"] == "running"
    assert report["finished_at"] is None and report["inserted_count"] == 1
    assert report["market_outcomes"][first]["snapshot_id"] == saved["snapshot_id"]
    assert report["market_outcomes"][second]["outcome"] == "unknown"
    with postgres_engine.connect() as connection:
        assert connection.execute(select(ingestion_runs.c.inserted_count).where(ingestion_runs.c.id == run_id)).scalar_one() == 0
    # An unknown outcome must also prevent a later report from claiming success.
    assert finish_run(postgres_engine, run_id)["status"] == "incomplete"


@pytest.mark.postgres
def test_grouped_replay_preserves_original_owners(postgres_engine, cycle_tickers):
    originals = {}
    first_run = start_run(postgres_engine, BASE, cycle_tickers, {"interval_seconds": 60})
    for ticker in cycle_tickers:
        originals[ticker] = save_in_run(postgres_engine, first_run, metadata(ticker), normalize_orderbook(BOOK, ticker, BASE), BASE, {})
    assert finish_run(postgres_engine, first_run)["inserted"] == 2
    second_run = start_run(postgres_engine, BASE, cycle_tickers, {"interval_seconds": 60})
    for ticker in cycle_tickers:
        saved = save_in_run(postgres_engine, second_run, metadata(ticker), normalize_orderbook(BOOK, ticker, BASE), BASE, {})
        assert saved["outcome"] == "duplicate" and saved["snapshot_id"] == originals[ticker]["snapshot_id"]
    summary = finish_run(postgres_engine, second_run)
    assert summary["status"] == "success" and summary["duplicate"] == 2 and summary["inserted"] == 0
    with postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(snapshots).where(snapshots.c.ingestion_run_id == first_run)).scalar_one() == 2
        assert connection.execute(select(func.count()).select_from(snapshots).where(snapshots.c.ingestion_run_id == second_run)).scalar_one() == 0


@pytest.mark.postgres
def test_cycle_budget_saves_received_data_and_marks_unvisited_market(postgres_engine, cycle_tickers):
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/orderbook"):
            clock.advance(3)  # This in-flight mock response exceeds the 2s cycle allowance.
            return httpx.Response(200, json=BOOK)
        return httpx.Response(200, json={"market": metadata(request.url.path.split("/")[-1])})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        summary = worker.collect_cycle(client, postgres_engine, cycle_tickers, BASE, 60, clock,
                                       cycle_budget_seconds=2, now=clock.now, monotonic=clock.monotonic)
    assert len(calls) == 2
    assert summary["status"] == "partial" and summary["inserted"] == 1 and summary["not_attempted"] == 1
    report = read_run(postgres_engine, summary["run_id"])
    assert report["market_outcomes"][cycle_tickers[1]] == {
        "outcome": "not_attempted", "attempted": False, "reason": "cycle_budget_exhausted",
    }


@pytest.mark.postgres
def test_retry_after_too_long_records_failure_and_skips_remaining_markets(postgres_engine, cycle_tickers):
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        summary = worker.collect_cycle(client, postgres_engine, cycle_tickers, BASE, 60, clock,
                                       cycle_budget_seconds=5, now=clock.now, monotonic=clock.monotonic)
    report = read_run(postgres_engine, summary["run_id"])
    assert len(calls) == 1 and clock.waits == []
    assert summary["status"] == "failed" and summary["failed"] == 1 and summary["not_attempted"] == 1
    first, second = [report["market_outcomes"][ticker] for ticker in cycle_tickers]
    assert first["outcome"] == "fetch_failed" and first["request_attempts"] == {"metadata": 1, "book": 0}
    assert second["outcome"] == "not_attempted" and second["reason"] == "retry_after_exceeds_budget"


@pytest.mark.postgres
def test_retry_success_keeps_one_slot_and_one_snapshot(postgres_engine, cycle_tickers):
    clock, calls = FakeTime(), []
    ticker = cycle_tickers[0]

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "1"})
        return httpx.Response(200, json=BOOK if request.url.path.endswith("/orderbook") else {"market": metadata(ticker)})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        summary = worker.collect_cycle(client, postgres_engine, [ticker], BASE, 60, clock, now=clock.now, monotonic=clock.monotonic)
    report = read_run(postgres_engine, summary["run_id"])
    assert summary["status"] == "success" and summary["inserted"] == 1
    assert len(calls) == 3 and clock.waits == [1]
    assert report["market_outcomes"][ticker]["request_attempts"] == {"metadata": 2, "book": 1}
    with postgres_engine.connect() as connection:
        row = connection.execute(select(snapshots).where(snapshots.c.ingestion_run_id == summary["run_id"])).mappings().one()
        assert row["collection_slot"] == BASE


@pytest.mark.postgres
def test_commit_acknowledgement_error_does_not_overwrite_committed_outcome(postgres_engine, cycle_tickers, monkeypatch):
    original = worker.save_in_run

    def committed_then_error(*args):
        original(*args)
        raise OperationalError("commit", {}, Exception("Synthetic lost acknowledgement"))

    monkeypatch.setattr(worker, "save_in_run", committed_then_error)
    ticker = cycle_tickers[0]
    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=BOOK if request.url.path.endswith("/orderbook") else {"market": metadata(ticker)}),
    )) as client:
        summary = worker.collect_cycle(client, postgres_engine, [ticker], BASE, 60, FakeTime())
    assert summary["status"] == "success" and summary["inserted"] == 1 and summary["failed"] == 0
    assert read_run(postgres_engine, summary["run_id"])["market_outcomes"][ticker]["outcome"] == "inserted"


@pytest.mark.postgres
def test_lost_failure_record_leaves_unknown_incomplete_without_blocking_other_market(postgres_engine, cycle_tickers, monkeypatch):
    original_save, original_record = worker.save_in_run, worker.record_outcome

    def save(engine, run_id, market, quote, slot, info):
        if quote.ticker == cycle_tickers[0]:
            raise OperationalError("insert", {}, Exception("Synthetic storage outage"))
        return original_save(engine, run_id, market, quote, slot, info)

    def record(engine, run_id, ticker, outcome):
        if outcome["outcome"] == "storage_failed":
            raise OperationalError("update", {}, Exception("Synthetic audit outage"))
        return original_record(engine, run_id, ticker, outcome)

    monkeypatch.setattr(worker, "save_in_run", save)
    monkeypatch.setattr(worker, "record_outcome", record)
    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=BOOK if request.url.path.endswith("/orderbook") else {"market": metadata(request.url.path.split("/")[-1])}),
    )) as client:
        summary = worker.collect_cycle(client, postgres_engine, cycle_tickers, BASE, 60, FakeTime())
    report = read_run(postgres_engine, summary["run_id"])
    assert summary["status"] == "incomplete" and summary["inserted"] == 1
    assert report["market_outcomes"][cycle_tickers[0]]["outcome"] == "unknown"
    assert report["market_outcomes"][cycle_tickers[1]]["outcome"] == "inserted"

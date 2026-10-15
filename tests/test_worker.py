"""Synthetic worker checks; fake clocks never sleep or contact the exchange."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import signal
import sys
from threading import Event
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from fetch_raw import BASE_URL, TIMEOUT
from models import ingestion_runs, markets, snapshots
from normalize_quote import normalize_orderbook
from persistence import minute_slot, read_observation, save_observation
from run_records import read_run
import worker


BASE = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
BOOKS = json.loads((Path(__file__).parent / "fixtures" / "orderbooks.json").read_text())


def metadata(ticker, status="active", title="Synthetic worker test"):
    return {"ticker": ticker, "title": title, "status": status, "market_type": "binary", "close_time": None}


@pytest.mark.parametrize("contents", [
    "not json", "{}", "[]", '["FIXTURE", "FIXTURE"]', '["FIXTURE", " FIXTURE "]',
    '[""]', '["  "]', '[12]', json.dumps([f"FIXTURE-{i}" for i in range(11)]),
])
def test_invalid_watchlists_are_rejected(tmp_path, contents):
    path = tmp_path / "watchlist.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        worker.load_watchlist(path)


def test_watchlist_accepts_small_setup_and_ten_markets(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text('[" FIXTURE-ONE "]')
    assert worker.load_watchlist(path) == ["FIXTURE-ONE"]
    tickers = [f"FIXTURE-{i}" for i in range(10)]
    path.write_text(json.dumps(tickers))
    assert worker.load_watchlist(path) == tickers


@pytest.mark.parametrize("interval, expected", [(10, (1, 30)), (60, (1, 0)), (90, (1, 30)), (3600, (0, 0))])
def test_collection_slot_floors_utc_time_for_general_intervals(interval, expected):
    at = BASE + timedelta(minutes=1, seconds=35, microseconds=123456)
    local = at.astimezone(timezone(timedelta(hours=-7)))
    assert worker.collection_slot(local, interval) == BASE + timedelta(minutes=expected[0], seconds=expected[1])
    assert worker.collection_slot(at, 60) == minute_slot(at)


@pytest.mark.parametrize("interval", [0, -1, 3601, True, 1.5])
def test_invalid_intervals_are_rejected(interval):
    with pytest.raises(ValueError):
        worker.collection_slot(BASE, interval)


def test_slot_requires_a_timezone():
    with pytest.raises(ValueError, match="timezone"):
        worker.collection_slot(BASE.replace(tzinfo=None), 60)


@pytest.mark.postgres
def test_cycle_is_sequential_with_shared_slot_and_separate_observation_times(monkeypatch, postgres_engine, worker_markets):
    requests, saved = [], []

    def handler(request):
        requests.append(request.url.path)
        ticker = request.url.path.split("/")[-2] if request.url.path.endswith("/orderbook") else request.url.path.split("/")[-1]
        return httpx.Response(200, json=BOOKS["two_sided"] if request.url.path.endswith("/orderbook") else {"market": metadata(ticker)})

    original_fetch = worker.fetch_book_quote

    def timed_quote(client, ticker):
        return original_fetch(client, ticker).model_copy(update={"observed_at": BASE + timedelta(seconds=len(saved) + 1)})

    original_save = worker.save_in_run

    def save(engine, run_id, market, quote, slot, info):
        result = original_save(engine, run_id, market, quote, slot, info)
        saved.append((market, quote, slot))
        return result

    monkeypatch.setattr(worker, "fetch_book_quote", timed_quote)
    monkeypatch.setattr(worker, "save_in_run", save)
    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        counts = worker.collect_cycle(client, postgres_engine, worker_markets, BASE, 10, Event())
    assert requests == [f"/trade-api/v2/markets/{worker_markets[0]}", f"/trade-api/v2/markets/{worker_markets[0]}/orderbook",
                        f"/trade-api/v2/markets/{worker_markets[1]}", f"/trade-api/v2/markets/{worker_markets[1]}/orderbook"]
    assert counts["inserted"] == counts["attempted"] == 2
    assert [item[2] for item in saved] == [BASE, BASE]
    assert read_run(postgres_engine, counts["run_id"])["watchlist_config"]["interval_seconds"] == 10
    assert [item[1].observed_at for item in saved] == [BASE + timedelta(seconds=1), BASE + timedelta(seconds=2)]


@pytest.mark.postgres
def test_failures_are_classified_and_nonactive_books_are_skipped(monkeypatch, postgres_engine, worker_markets, caplog):
    calls, refreshed, saved = [], [], []
    tickers = [worker_markets[0] + suffix for suffix in ("-HTTP", "-INVALID", "-DB", "-GOOD", "-CLOSED")]
    worker_markets.extend(tickers)

    def handler(request):
        path = request.url.path
        calls.append(path)
        ticker = path.split("/")[-2] if path.endswith("/orderbook") else path.split("/")[-1]
        if ticker.endswith("-HTTP"):
            return httpx.Response(503)
        if path.endswith("/orderbook"):
            return httpx.Response(200, json=BOOKS["missing_side"] if ticker.endswith("-INVALID") else BOOKS["two_sided"])
        return httpx.Response(200, json={"market": metadata(ticker, "closed" if ticker.endswith("-CLOSED") else "active")})

    original_save, original_inactive = worker.save_in_run, worker.save_inactive_in_run

    def save(engine, run_id, market, quote, slot, info):
        if quote.ticker.endswith("-DB"):
            raise OperationalError("insert", {}, Exception("private-password"))
        saved.append(quote.ticker)
        return original_save(engine, run_id, market, quote, slot, info)

    def inactive(engine, run_id, market, ticker, info):
        refreshed.append(ticker)
        original_inactive(engine, run_id, market, ticker, info)

    monkeypatch.setattr(worker, "save_in_run", save)
    monkeypatch.setattr(worker, "save_inactive_in_run", inactive)
    clock = FakeClock()
    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        counts = worker.collect_cycle(client, postgres_engine, tickers, BASE, 60, FakeStop(clock), now=clock.now, monotonic=clock.monotonic)
    assert counts["status"] == "partial" and counts["failed"] == 3 and counts["inserted"] == 1
    assert counts["not_active"] == 1 and counts["attempted"] == 5
    assert saved == [tickers[3]] and refreshed == [tickers[4]]
    assert sum(path.endswith(tickers[0]) for path in calls) == 3
    assert not any(path.endswith(tickers[4] + "/orderbook") for path in calls)
    outcomes = read_run(postgres_engine, counts["run_id"])["market_outcomes"]
    assert [outcomes[ticker]["outcome"] for ticker in tickers] == ["fetch_failed", "validation_failed", "storage_failed", "inserted", "not_attempted"]
    assert "private-password" not in caplog.text


@pytest.mark.postgres
def test_stop_keeps_current_commit_and_records_incomplete_cycle(monkeypatch, postgres_engine, worker_markets):
    stop = Event()
    calls = []
    monkeypatch.setattr(worker, "fetch_market", lambda client, ticker: metadata(ticker))
    monkeypatch.setattr(worker, "fetch_book_quote", lambda client, ticker: normalize_orderbook(BOOKS["two_sided"], ticker, BASE))

    original_save = worker.save_in_run

    def save(engine, run_id, market, quote, slot, info):
        result = original_save(engine, run_id, market, quote, slot, info)
        calls.append(quote.ticker)
        stop.set()
        return result

    monkeypatch.setattr(worker, "save_in_run", save)
    counts = worker.collect_cycle(None, postgres_engine, worker_markets, BASE, 60, stop)
    assert calls == [worker_markets[0]]
    assert counts["attempted"] == 1 and counts["not_attempted"] == 1
    assert counts["status"] == "incomplete"


class FakeClock:
    def __init__(self):
        self.at = BASE + timedelta(seconds=5)
        self.elapsed = 0

    def now(self):
        return self.at

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)
        self.elapsed += seconds


class FakeStop:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False
        self.waits = []
        self.wall_jump = 0
        self.stop_on_wait = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.stop_on_wait:
            self.stopped = True
            return True
        self.clock.advance(seconds)
        self.clock.at += timedelta(seconds=self.wall_jump)
        self.wall_jump = 0
        return False


@pytest.mark.parametrize("first_duration, wall_jump, expected_slots", [
    (2, 0, [0, 1]), (137, 0, [0, 2]), (2, 180, [0, 4]), (2, -90, [0, 1]),
])
def test_scheduler_waits_and_skips_elapsed_slots_without_overlap(first_duration, wall_jump, expected_slots, monkeypatch):
    clock, slots = FakeClock(), []
    stop = FakeStop(clock)
    stop.wall_jump = wall_jump

    def collect(client, engine, tickers, slot, interval, event, **kwargs):
        slots.append(slot)
        clock.advance(first_duration if len(slots) == 1 else 2)
        return {"failed": 0}

    monkeypatch.setattr(worker, "collect_cycle", collect)
    assert worker.run_cycles(None, None, ["FIXTURE"], 60, 2, stop, now=clock.now, monotonic=clock.monotonic) == 0
    assert slots == [BASE + timedelta(minutes=value) for value in expected_slots]
    if first_duration == 2 and wall_jump == 0:
        assert stop.waits == [53]


def test_stop_wakes_wait_without_another_cycle(monkeypatch):
    clock, calls = FakeClock(), []
    stop = FakeStop(clock)
    stop.stop_on_wait = True

    def collect(*args, **kwargs):
        calls.append(args[3])
        return {"failed": 1}

    monkeypatch.setattr(worker, "collect_cycle", collect)
    assert worker.run_cycles(None, None, ["FIXTURE"], 60, None, stop, now=clock.now, monotonic=clock.monotonic) == 1
    assert calls == [BASE]


def test_restart_starts_current_slot_without_past_catchup(monkeypatch):
    clock, slots = FakeClock(), []
    clock.advance(3600)
    monkeypatch.setattr(worker, "collect_cycle", lambda client, engine, tickers, slot, interval, stop, **kwargs: slots.append(slot) or {"failed": 0})
    stop = FakeStop(clock)
    assert worker.run_cycles(None, None, ["FIXTURE"], 60, 1, stop, now=clock.now, monotonic=clock.monotonic) == 0
    assert slots == [BASE + timedelta(hours=1)]


@pytest.mark.parametrize("failure", [False, True])
def test_command_closes_resources_and_restores_signal_handlers(tmp_path, monkeypatch, failure):
    path = tmp_path / "watchlist.json"
    path.write_text('["FIXTURE"]')
    client = httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    class Engine:
        disposed = False

        def dispose(self):
            self.disposed = True

    engine = Engine()
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(worker, "make_engine", lambda: engine)
    monkeypatch.setattr(worker.httpx, "Client", lambda **kwargs: client)

    def run(*args, **kwargs):
        if failure:
            raise ValueError("Synthetic failure")
        return 0

    monkeypatch.setattr(worker, "run_cycles", run)
    monkeypatch.setattr(sys, "argv", ["worker.py", "--watchlist", str(path), "--cycles", "1"])
    assert worker.main() == (1 if failure else 0)
    assert client.is_closed and engine.disposed
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers


@pytest.fixture
def worker_markets(postgres_engine):
    tickers = [f"FIXTURE-WORKER-{uuid4()}" for _ in range(2)]
    yield tickers
    with postgres_engine.begin() as connection:
        ids = select(markets.c.id).where(markets.c.external_id.in_(tickers))
        connection.execute(snapshots.delete().where(snapshots.c.market_id.in_(ids)))
        for ticker in tickers:
            connection.execute(ingestion_runs.delete().where(ingestion_runs.c.watchlist_config.contains({"watchlist": [ticker]})))
        connection.execute(markets.delete().where(markets.c.external_id.in_(tickers)))


@pytest.mark.postgres
def test_worker_saves_shared_slots_replays_and_new_unchanged_prices(postgres_engine, worker_markets):
    def handler(request):
        ticker = request.url.path.split("/")[-1]
        return httpx.Response(200, json=BOOKS["subpenny"] if ticker == "orderbook" else {"market": metadata(ticker)})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        first = worker.collect_cycle(client, postgres_engine, worker_markets, BASE, 10, Event())
        replay = worker.collect_cycle(client, postgres_engine, worker_markets, BASE, 10, Event())
        later = worker.collect_cycle(client, postgres_engine, worker_markets, BASE + timedelta(seconds=10), 10, Event())
    assert first["inserted"] == replay["duplicate"] == later["inserted"] == 2
    with postgres_engine.connect() as connection:
        rows = connection.execute(select(snapshots, markets.c.external_id).join(markets).where(markets.c.external_id.in_(worker_markets))).mappings().all()
        assert len(rows) == 4
        assert {row["collection_slot"] for row in rows} == {BASE, BASE + timedelta(seconds=10)}
        runs = connection.execute(select(ingestion_runs).where(ingestion_runs.c.watchlist_config.contains({"interval_seconds": 10}))).mappings().all()
        owned = [run for run in runs if run["watchlist_config"]["watchlist"][0] in worker_markets]
        assert len(owned) == 3
        assert all(run["watchlist_config"]["watchlist"] == worker_markets for run in owned)
        assert sum(run["inserted_count"] for run in owned) == 4
        assert sum(run["duplicate_count"] for run in owned) == 2
        assert all(row["yes_bid"] == Decimal("0.4201") for row in rows)


@pytest.mark.postgres
def test_closing_market_refreshes_metadata_and_preserves_history(postgres_engine, worker_markets):
    ticker = worker_markets[0]
    status = "active"
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=BOOKS["two_sided"] if request.url.path.endswith("/orderbook") else {"market": metadata(ticker, status, f"Synthetic {status}")})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        worker.collect_cycle(client, postgres_engine, [ticker], BASE, 60, Event())
        with postgres_engine.connect() as connection:
            market_id = connection.execute(select(markets.c.id).where(markets.c.external_id == ticker)).scalar_one()
            snapshot_id = connection.execute(select(snapshots.c.id).where(snapshots.c.market_id == market_id)).scalar_one()
        original = read_observation(postgres_engine, snapshot_id)["quote"]
        status = "closed"
        for offset in (1, 2):
            counts = worker.collect_cycle(client, postgres_engine, [ticker], BASE + timedelta(minutes=offset), 60, Event())
            assert counts["not_active"] == 1 and counts["inserted"] == 0
    assert sum(path.endswith("/orderbook") for path in paths) == 1
    result = read_observation(postgres_engine, snapshot_id)
    assert result["market_status"] == "closed" and result["title"] == "Synthetic closed"
    assert result["quote"] == original
    with postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(snapshots).where(snapshots.c.market_id == market_id)).scalar_one() == 1


@pytest.mark.postgres
def test_worker_storage_failure_keeps_old_data_and_continues(postgres_engine, worker_markets, monkeypatch):
    first, second = worker_markets
    initial = save_observation(postgres_engine, metadata(first), normalize_orderbook(BOOKS["two_sided"], first, BASE), BASE)
    original_save = worker.save_in_run

    def save(engine, run_id, market, quote, slot, info):
        if quote.ticker == first:
            quote = quote.model_copy(update={"yes_bid": Decimal("1.1")})
        return original_save(engine, run_id, market, quote, slot, info)

    monkeypatch.setattr(worker, "save_in_run", save)

    def handler(request):
        ticker = request.url.path.split("/")[-1]
        return httpx.Response(200, json=BOOKS["two_sided"] if ticker == "orderbook" else {"market": metadata(ticker, title="Synthetic new metadata")})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        counts = worker.collect_cycle(client, postgres_engine, worker_markets, BASE + timedelta(minutes=1), 60, Event())
    assert counts["failed"] == 1 and counts["inserted"] == 1
    assert read_observation(postgres_engine, initial["snapshot_id"])["title"] == "Synthetic worker test"
    with postgres_engine.connect() as connection:
        run = connection.execute(select(ingestion_runs).where(ingestion_runs.c.id == counts["run_id"])).mappings().one()
        assert run["status"] == "partial" and run["inserted_count"] == 1
        assert run["market_outcomes"][first]["outcome"] == "storage_failed"
        assert run["market_outcomes"][second]["outcome"] == "inserted"

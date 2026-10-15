"""Read-only API contracts; synthetic data stays in the test DB."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

import api
from fetch_quote import fetch_live_quote
from models import ingestion_runs, markets, snapshots
from normalize_quote import normalize_orderbook
from persistence import minute_slot, save_observation


class BrokenEngine:
    """An unavailable database with an intentionally sensitive error message."""

    disposed = False

    def connect(self):
        raise OperationalError("SELECT", {}, Exception("private-password"))

    def dispose(self):
        self.disposed = True


def test_health_survives_database_outage_and_engine_closes(monkeypatch):
    engine = BrokenEngine()
    monkeypatch.setattr(api, "make_engine", lambda: engine)
    with TestClient(api.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        for path in ("/ready", "/markets", "/markets/1", "/markets/1/quote", "/markets/1/history", "/alert-rules"):
            response = client.get(path)
            assert response.status_code == 503
            assert response.json() == {"detail": "Database unavailable"}
            assert "private-password" not in response.text
        for path, payload in (("/alert-rules", {"market_id": 1, "threshold": "0.40"}),
                              ("/alert-rules/1/disable", None)):
            response = client.post(path, json=payload)
            assert response.status_code == 503
            assert response.json() == {"detail": "Database unavailable"}
            assert "private-password" not in response.text
    assert engine.disposed


def test_readiness_rejects_a_database_without_the_market_table(monkeypatch):
    class MissingTableConnection:
        def execute(self, statement):
            raise OperationalError(str(statement), {}, Exception("relation markets does not exist"))

    class MissingTableEngine(BrokenEngine):
        @contextmanager
        def connect(self):
            yield MissingTableConnection()

    monkeypatch.setattr(api, "make_engine", MissingTableEngine)
    with TestClient(api.app) as client:
        assert client.get("/ready").status_code == 503
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize("path", [
    "/markets?limit=0", "/markets?limit=101", "/markets?limit=wrong",
    "/markets?offset=-1", "/markets?offset=wrong",
    "/markets?offset=9223372036854775808",
    "/markets/0", "/markets/-1", "/markets/not-an-id",
    "/markets/9223372036854775808",
    "/markets/0/quote", "/markets/not-an-id/quote", "/markets/9223372036854775808/quote",
    "/markets/0/history", "/markets/not-an-id/history", "/markets/9223372036854775808/history",
])
def test_invalid_inputs_are_rejected_before_query_execution(path):
    # No database query should execute for these invalid HTTP inputs.
    api.app.dependency_overrides[api.get_connection] = lambda: None
    try:
        with TestClient(api.app) as client:
            assert client.get(path).status_code == 422
    finally:
        api.app.dependency_overrides.clear()


def test_documentation_describes_implemented_routes_and_string_prices():
    with TestClient(api.app) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {
        "/health", "/ready", "/markets", "/markets/{id}", "/markets/{id}/quote", "/markets/{id}/history",
        "/alert-rules", "/alert-rules/{id}/disable",
    }
    parameters = {item["name"]: item["schema"] for item in schema["paths"]["/markets"]["get"]["parameters"]}
    assert parameters["limit"]["maximum"] == 100
    assert parameters["offset"]["minimum"] == 0
    history_parameters = {item["name"]: item["schema"] for item in schema["paths"]["/markets/{id}/history"]["get"]["parameters"]}
    assert history_parameters["limit"]["maximum"] == 1000
    assert history_parameters["limit"]["default"] == 100
    for field in ("yes_bid", "yes_ask", "yes_bid_quantity", "yes_ask_quantity", "spread"):
        options = schema["components"]["schemas"]["Quote"]["properties"][field]["anyOf"]
        assert {item["type"] for item in options} == {"string", "null"}


def test_dashboard_is_served_without_a_database(monkeypatch):
    monkeypatch.setattr(api, "make_engine", BrokenEngine)
    with TestClient(api.app) as client:
        root = client.get("/", follow_redirects=False)
        page = client.get("/dashboard")
    assert root.status_code == 307 and root.headers["location"] == "/dashboard"
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    for path in ("/markets?limit=100", "/markets/${m.id}/quote", "/markets/${id}/history"):
        assert path in page.text


@pytest.fixture
def api_client(postgres_engine, monkeypatch):
    monkeypatch.setattr(api, "make_engine", lambda: postgres_engine)

    def enforce_reads(connection, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().upper().startswith("SELECT"), "API requests must only read"

    event.listen(postgres_engine, "before_cursor_execute", enforce_reads)
    try:
        with TestClient(api.app) as client:
            yield client
    finally:
        event.remove(postgres_engine, "before_cursor_execute", enforce_reads)


@pytest.fixture
def stored_markets(postgres_engine):
    # Metadata-only synthetic markets intentionally have no snapshots.
    ids = []
    with postgres_engine.begin() as connection:
        for status in ("active", "closed", "settled"):
            ids.append(connection.execute(markets.insert().values(
                platform="kalshi", external_id=f"FIXTURE-API-{uuid4()}",
                title=f"Synthetic API test ({status})", status=status,
                close_time=None if status == "active" else datetime(2026, 10, 13, tzinfo=timezone.utc),
            ).returning(markets.c.id)).scalar_one())
    try:
        yield ids
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(markets.delete().where(markets.c.id.in_(ids)))


@pytest.mark.postgres
def test_readiness_with_real_postgres(api_client):
    response = api_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.postgres
def test_list_is_ordered_bounded_and_includes_closed_markets(stored_markets, api_client, postgres_engine):
    with postgres_engine.connect() as connection:
        expected_ids = list(connection.execute(select(markets.c.id).order_by(markets.c.id)).scalars())
    response = api_client.get("/markets?limit=100")
    assert response.status_code == 200
    result = response.json()
    assert [row["id"] for row in result] == expected_ids[:100]
    assert set(stored_markets).issubset({row["id"] for row in result})
    page = api_client.get("/markets?limit=1&offset=1").json()
    assert [row["id"] for row in page] == expected_ids[1:2]
    assert api_client.get(f"/markets?offset={len(expected_ids)}").json() == []


@pytest.mark.postgres
def test_detail_for_known_market_without_snapshots(stored_markets, api_client):
    response = api_client.get(f"/markets/{stored_markets[0]}")
    assert response.status_code == 200
    result = response.json()
    assert result["id"] == stored_markets[0]
    assert result["external_id"].startswith("FIXTURE-API-")
    assert result["title"] == "Synthetic API test (active)"
    assert result["close_time"] is None
    assert datetime.fromisoformat(result["updated_at"]).utcoffset().total_seconds() == 0


@pytest.mark.postgres
def test_closed_market_details_remain_available(stored_markets, api_client):
    response = api_client.get(f"/markets/{stored_markets[1]}")
    assert response.status_code == 200
    assert response.json()["status"] == "closed"
    assert datetime.fromisoformat(response.json()["close_time"]) == datetime(2026, 10, 13, tzinfo=timezone.utc)


@pytest.mark.postgres
def test_unknown_market_returns_404(api_client):
    for path in ("/markets/9223372036854775807", "/markets/9223372036854775807/quote", "/markets/9223372036854775807/history"):
        response = api_client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Market not found"}


BOOKS = json.loads((Path(__file__).parent / "fixtures" / "orderbooks.json").read_text())
OBSERVED_AT = datetime(2026, 10, 14, 12, 0, 14, tzinfo=timezone.utc)


@pytest.fixture
def saved_quote_factory(stored_markets, postgres_engine):
    """Commit synthetic snapshots before the HTTP read-only SQL guard starts."""
    with postgres_engine.connect() as connection:
        metadata = {row["id"]: dict(row) for row in connection.execute(select(markets).where(
            markets.c.id.in_(stored_markets),
        )).mappings()}

    def save(case="subpenny", index=0, observed_at=OBSERVED_AT, slot=None, source_timestamp=None):
        row = metadata[stored_markets[index]]
        market = {
            "ticker": row["external_id"], "title": row["title"], "status": row["status"],
            "market_type": "binary", "close_time": row["close_time"].isoformat() if row["close_time"] else None,
        }
        quote = normalize_orderbook(BOOKS[case], row["external_id"], observed_at)
        quote = quote.model_copy(update={"source_timestamp": source_timestamp})
        return save_observation(postgres_engine, market, quote, slot or minute_slot(observed_at))

    yield save
    # Remove only this fixture's snapshots and attempts, before metadata cleanup.
    with postgres_engine.begin() as connection:
        connection.execute(snapshots.delete().where(snapshots.c.market_id.in_(stored_markets)))
        for row in metadata.values():
            connection.execute(ingestion_runs.delete().where(
                ingestion_runs.c.watchlist_config.contains({"watchlist": [row["external_id"]]}),
            ))


@pytest.fixture
def saved_quote(saved_quote_factory):
    return saved_quote_factory(source_timestamp=OBSERVED_AT - timedelta(days=1))


@pytest.mark.postgres
def test_known_market_without_observations_returns_explicit_nulls(stored_markets, api_client):
    response = api_client.get(f"/markets/{stored_markets[0]}/quote")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "no_observations"
    assert result["market_status"] == "active"
    for field in ("quote", "snapshot_id", "ingestion_run_id", "collection_slot", "age_seconds", "is_stale"):
        assert result[field] is None


@pytest.mark.postgres
def test_saved_quote_age_changes_without_changing_stored_prices(saved_quote, stored_markets, api_client, monkeypatch):
    monkeypatch.setattr(api, "utc_now", lambda: OBSERVED_AT + timedelta(seconds=120))
    path = f"/markets/{stored_markets[0]}/quote"
    response = api_client.get(path)
    assert response.status_code == 200
    fresh = response.json()
    assert fresh["snapshot_id"] == saved_quote["snapshot_id"]
    assert fresh["age_seconds"] == 120 and fresh["is_stale"] is False
    assert fresh["quote"]["yes_bid"] == "0.4201"
    assert fresh["quote"]["yes_ask"] == "0.4498"
    assert fresh["quote"]["spread"] == "0.0297"
    assert fresh["quote"]["source_timestamp"] == "2026-10-13T12:00:14Z"
    monkeypatch.setattr(api, "utc_now", lambda: OBSERVED_AT + timedelta(seconds=120.1))
    stale = api_client.get(path).json()
    assert stale["is_stale"] is True and stale["age_seconds"] == 120.1
    assert stale["quote"] == fresh["quote"]
    assert stale["snapshot_id"] == fresh["snapshot_id"]


@pytest.mark.postgres
@pytest.mark.parametrize("case, status", [
    ("two_sided", "two_sided"), ("yes_only", "one_sided"), ("no_only", "one_sided"),
    ("empty", "empty_book"), ("crossed", "crossed"),
])
def test_quote_liquidity_status_preserves_prices(case, status, saved_quote_factory, stored_markets, monkeypatch, request):
    saved_quote_factory(case)
    monkeypatch.setattr(api, "utc_now", lambda: OBSERVED_AT + timedelta(seconds=60))
    # Finish setup writes before activating the API client's SELECT-only guard.
    client = request.getfixturevalue("api_client")
    response = client.get(f"/markets/{stored_markets[0]}/quote")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == status
    assert result["age_seconds"] == 60 and result["is_stale"] is False
    expected = normalize_orderbook(BOOKS[case], result["ticker"], OBSERVED_AT)
    assert result["quote"] == expected.model_dump(mode="json")
    if case == "empty":
        assert result["quote"] is not None
        assert result["quote"]["yes_bid"] is None and result["quote"]["yes_ask"] is None
    if case == "crossed":
        assert result["quote"]["spread"] == "-0.0200" and result["quote"]["is_crossed"] is True


@pytest.mark.postgres
@pytest.mark.parametrize("threshold, stale", [(60, True), (180, False)])
def test_configurable_threshold_changes_freshness(threshold, stale, saved_quote, stored_markets, monkeypatch, request):
    monkeypatch.setenv("STALE_AFTER_SECONDS", str(threshold))
    monkeypatch.setattr(api, "utc_now", lambda: OBSERVED_AT + timedelta(seconds=90))
    client = request.getfixturevalue("api_client")
    result = client.get(f"/markets/{stored_markets[0]}/quote").json()
    assert result["stale_after_seconds"] == threshold
    assert result["age_seconds"] == 90 and result["is_stale"] is stale


@pytest.mark.postgres
def test_latest_uses_observation_time_then_id(saved_quote_factory, stored_markets, request):
    newest_time = OBSERVED_AT + timedelta(minutes=2)
    newer = saved_quote_factory(observed_at=newest_time)
    older = saved_quote_factory(observed_at=OBSERVED_AT)
    assert older["snapshot_id"] > newer["snapshot_id"]
    client = request.getfixturevalue("api_client")
    assert client.get(f"/markets/{stored_markets[0]}/quote").json()["snapshot_id"] == newer["snapshot_id"]


@pytest.mark.postgres
def test_latest_breaks_equal_observation_times_by_id(saved_quote_factory, stored_markets, request):
    observed = OBSERVED_AT + timedelta(minutes=2)
    first = saved_quote_factory(observed_at=observed, slot=minute_slot(OBSERVED_AT))
    second = saved_quote_factory(observed_at=observed, slot=minute_slot(OBSERVED_AT) + timedelta(minutes=1))
    assert second["snapshot_id"] > first["snapshot_id"]
    client = request.getfixturevalue("api_client")
    assert client.get(f"/markets/{stored_markets[0]}/quote").json()["snapshot_id"] == second["snapshot_id"]


@pytest.mark.postgres
def test_closed_market_retains_its_quote(saved_quote_factory, stored_markets, request):
    saved = saved_quote_factory(index=1)
    client = request.getfixturevalue("api_client")
    response = client.get(f"/markets/{stored_markets[1]}/quote")
    assert response.status_code == 200
    assert response.json()["market_status"] == "closed"
    assert response.json()["snapshot_id"] == saved["snapshot_id"]


def test_readiness_checks_snapshot_table_as_well(monkeypatch):
    class MissingSnapshotsConnection:
        def execute(self, statement):
            if "snapshots" in str(statement):
                raise OperationalError(str(statement), {}, Exception("relation snapshots does not exist"))
            return self

        def first(self):
            return None

    class MissingSnapshotsEngine(BrokenEngine):
        @contextmanager
        def connect(self):
            yield MissingSnapshotsConnection()

    monkeypatch.setattr(api, "make_engine", MissingSnapshotsEngine)
    with TestClient(api.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503


@pytest.mark.parametrize("parameters", [
    {"limit": 0}, {"limit": 1001}, {"limit": "bad"}, {"limit": "1.5"},
    {"offset": -1}, {"offset": "bad"}, {"offset": 9223372036854775808},
    {"start": "bad"}, {"end": "bad"}, {"start": "2026-10-14T12:00:00"},
    {"end": "2026-10-14"},
    {"start": "2026-10-14T12:00:00Z", "end": "2026-10-14T11:00:00Z"},
    {"start": "2026-10-14T12:00:00Z", "end": "2026-10-14T12:00:00Z"},
    {"start": "2026-10-01T00:00:00Z", "end": "2026-10-09T00:00:00Z"},
    {"start": "2026-10-01T00:00:00Z", "end": "2026-10-08T00:00:00.000001Z"},
    {"end": "0001-01-01T00:00:00Z"},
])
def test_history_rejects_invalid_inputs_without_queries(parameters):
    api.app.dependency_overrides[api.get_connection] = lambda: None
    try:
        with TestClient(api.app) as client:
            response = client.get("/markets/1/history", params=parameters)
            assert response.status_code == 422
    finally:
        api.app.dependency_overrides.clear()


@pytest.fixture
def history_samples(saved_quote_factory):
    start = OBSERVED_AT
    end = start + timedelta(minutes=4)
    # Insert out of chronological order; ties use distinct original slots.
    saved_quote_factory(observed_at=end)
    saved_quote_factory(observed_at=start - timedelta(minutes=1))
    at_start = saved_quote_factory(observed_at=start)
    tie_time = start + timedelta(minutes=3)
    first_tie = saved_quote_factory(observed_at=tie_time, slot=minute_slot(start) + timedelta(minutes=1))
    second_tie = saved_quote_factory(observed_at=tie_time, slot=minute_slot(start) + timedelta(minutes=2))
    return start, end, [at_start["snapshot_id"], first_tie["snapshot_id"], second_tie["snapshot_id"]]


@pytest.mark.postgres
def test_history_filters_boundaries_orders_and_pages(history_samples, stored_markets, api_client):
    start, end, ids = history_samples
    path = f"/markets/{stored_markets[0]}/history"
    parameters = {"start": start.isoformat(), "end": end.isoformat()}
    response = api_client.get(path, params=parameters)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"
    assert [row["snapshot_id"] for row in result["observations"]] == ids
    quote = result["observations"][0]["quote"]
    assert quote["yes_bid"] == "0.4201" and quote["yes_ask"] == "0.4498"
    assert quote["spread"] == "0.0297"
    page = api_client.get(path, params={**parameters, "limit": 1, "offset": 1}).json()
    assert page["limit"] == 1 and page["offset"] == 1
    assert [row["snapshot_id"] for row in page["observations"]] == ids[1:2]
    empty_page = api_client.get(path, params={**parameters, "offset": 3}).json()
    assert empty_page["observations"] == [] and empty_page["status"] == "ok"
    # The exclusive end belongs to the adjacent range, not both ranges.
    adjacent = api_client.get(path, params={"start": end.isoformat(), "end": (end + timedelta(minutes=1)).isoformat()}).json()
    assert len(adjacent["observations"]) == 1
    assert adjacent["observations"][0]["quote"]["observed_at"] == end.isoformat().replace("+00:00", "Z")


@pytest.mark.postgres
def test_history_normalizes_timezone_offsets(history_samples, stored_markets, api_client):
    start, end, ids = history_samples
    local_zone = timezone(timedelta(hours=-7))
    response = api_client.get(f"/markets/{stored_markets[0]}/history", params={
        "start": start.astimezone(local_zone).isoformat(), "end": end.astimezone(local_zone).isoformat(),
    })
    assert response.status_code == 200
    result = response.json()
    assert result["start"] == start.isoformat().replace("+00:00", "Z")
    assert result["end"] == end.isoformat().replace("+00:00", "Z")
    assert [row["snapshot_id"] for row in result["observations"]] == ids


@pytest.mark.postgres
def test_history_default_window_includes_only_last_24_hours(saved_quote_factory, stored_markets, monkeypatch, request):
    start = minute_slot(OBSERVED_AT)
    end = start + timedelta(days=1)
    saved_quote_factory(observed_at=start - timedelta(seconds=1))
    first = saved_quote_factory(observed_at=start)
    last = saved_quote_factory(observed_at=end - timedelta(microseconds=1))
    saved_quote_factory(observed_at=end)
    monkeypatch.setattr(api, "utc_now", lambda: end)
    client = request.getfixturevalue("api_client")
    result = client.get(f"/markets/{stored_markets[0]}/history").json()
    assert result["start"] == start.isoformat().replace("+00:00", "Z")
    assert result["end"] == end.isoformat().replace("+00:00", "Z")
    assert [row["snapshot_id"] for row in result["observations"]] == [first["snapshot_id"], last["snapshot_id"]]


@pytest.mark.postgres
def test_history_known_market_without_snapshots_is_not_404(stored_markets, api_client):
    response = api_client.get(f"/markets/{stored_markets[0]}/history")
    assert response.status_code == 200
    assert response.json()["status"] == "no_observations"
    assert response.json()["observations"] == []


@pytest.mark.postgres
def test_history_empty_range_is_not_no_observations(saved_quote, stored_markets, api_client):
    response = api_client.get(f"/markets/{stored_markets[0]}/history", params={
        "start": (OBSERVED_AT + timedelta(days=1)).isoformat(),
        "end": (OBSERVED_AT + timedelta(days=2)).isoformat(),
    })
    assert response.status_code == 200
    assert response.json()["observations"] == []
    assert response.json()["status"] == "ok"


@pytest.mark.postgres
@pytest.mark.parametrize("case", ["empty", "yes_only", "crossed"])
def test_history_preserves_missing_and_crossed_prices(case, saved_quote_factory, stored_markets, request):
    saved_quote_factory(case, index=1)
    client = request.getfixturevalue("api_client")
    response = client.get(f"/markets/{stored_markets[1]}/history", params={
        "start": OBSERVED_AT.isoformat(), "end": (OBSERVED_AT + timedelta(minutes=1)).isoformat(),
    })
    assert response.status_code == 200
    result = response.json()
    assert result["market_status"] == "closed"
    expected = normalize_orderbook(BOOKS[case], result["ticker"], OBSERVED_AT)
    assert result["observations"][0]["quote"] == expected.model_dump(mode="json")


@pytest.mark.postgres
def test_history_limits_actual_rows_to_1000(saved_quote, stored_markets, postgres_engine, request):
    # 1,001 synthetic rows in the test database only, not a live collection metric.
    with postgres_engine.begin() as connection:
        original = dict(connection.execute(select(snapshots).where(snapshots.c.id == saved_quote["snapshot_id"])).mappings().one())
        rows = []
        for index in range(1, 1001):
            slot = minute_slot(OBSERVED_AT) + timedelta(minutes=index)
            rows.append({key: value for key, value in original.items() if key != "id"})
            rows[-1].update(collection_slot=slot, observed_at=slot + timedelta(seconds=14))
        connection.execute(snapshots.insert(), rows)
        connection.execute(ingestion_runs.update().where(ingestion_runs.c.id == saved_quote["run_id"]).values(inserted_count=1001))
    client = request.getfixturevalue("api_client")
    parameters = {"start": OBSERVED_AT.isoformat(), "end": (OBSERVED_AT + timedelta(days=1)).isoformat(), "limit": 1000}
    path = f"/markets/{stored_markets[0]}/history"
    response = client.get(path, params=parameters)
    assert response.status_code == 200
    result = response.json()
    assert len(result["observations"]) == 1000
    assert len({row["snapshot_id"] for row in result["observations"]}) == 1000
    last_page = client.get(path, params={**parameters, "offset": 1000}).json()
    assert len(last_page["observations"]) == 1
    assert last_page["observations"][0]["snapshot_id"] not in {row["snapshot_id"] for row in result["observations"]}


@pytest.mark.postgres
def test_history_does_not_include_another_markets_snapshots(saved_quote_factory, stored_markets, request):
    first = saved_quote_factory(index=0)
    second = saved_quote_factory("crossed", index=1)
    client = request.getfixturevalue("api_client")
    response = client.get(f"/markets/{stored_markets[0]}/history", params={
        "start": OBSERVED_AT.isoformat(), "end": (OBSERVED_AT + timedelta(minutes=1)).isoformat(),
    })
    assert response.status_code == 200
    assert [row["snapshot_id"] for row in response.json()["observations"]] == [first["snapshot_id"]]
    assert second["snapshot_id"] != first["snapshot_id"]


@pytest.mark.postgres
def test_api_serves_saved_data_after_simulated_upstream_failure(saved_quote, stored_markets, api_client, monkeypatch):
    # Controlled synthetic 503, not a claim that the real exchange is down.
    with httpx.Client(base_url="https://fixture.invalid/", transport=httpx.MockTransport(
        lambda request: httpx.Response(503),
    )) as exchange:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_live_quote(exchange, "FIXTURE-UNAVAILABLE")
    monkeypatch.setattr(api, "utc_now", lambda: OBSERVED_AT + timedelta(seconds=180))
    latest = api_client.get(f"/markets/{stored_markets[0]}/quote")
    assert latest.status_code == 200
    assert latest.json()["snapshot_id"] == saved_quote["snapshot_id"]
    assert latest.json()["is_stale"] is True
    history = api_client.get(f"/markets/{stored_markets[0]}/history")
    assert history.status_code == 200
    assert [row["snapshot_id"] for row in history.json()["observations"]] == [saved_quote["snapshot_id"]]

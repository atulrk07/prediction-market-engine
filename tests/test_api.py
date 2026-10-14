"""Step 3A HTTP contracts; synthetic integration data stays in the test DB."""

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

import api
from models import markets


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
        for path in ("/ready", "/markets", "/markets/1"):
            response = client.get(path)
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
])
def test_invalid_inputs_are_rejected_before_query_execution(path):
    # No database query should execute for these invalid HTTP inputs.
    api.app.dependency_overrides[api.get_connection] = lambda: None
    try:
        with TestClient(api.app) as client:
            assert client.get(path).status_code == 422
    finally:
        api.app.dependency_overrides.clear()


def test_documentation_describes_only_step_3a_routes():
    with TestClient(api.app) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {"/health", "/ready", "/markets", "/markets/{id}"}
    parameters = {item["name"]: item["schema"] for item in schema["paths"]["/markets"]["get"]["parameters"]}
    assert parameters["limit"]["maximum"] == 100
    assert parameters["offset"]["minimum"] == 0


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
    response = api_client.get("/markets/9223372036854775807")
    assert response.status_code == 404
    assert response.json() == {"detail": "Market not found"}

"""Price alerts use synthetic PostgreSQL rows and mocked Discord HTTP only."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Event
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError, OperationalError

import alerts
import api
from models import alert_rules, ingestion_runs, markets, snapshots
from normalize_quote import normalize_orderbook
from persistence import save_observation
from run_records import read_run
import worker


BASE = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
WEBHOOK = "https://discord.invalid/api/webhooks/FIXTURE/secret-token"


@pytest.fixture
def discord_http(monkeypatch):
    """A fake URL opener, never an actual Discord connection."""
    state = SimpleNamespace(calls=[], outcomes=[], on_request=None)

    class Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_request(request, *, timeout):
        state.calls.append((request, timeout))
        if state.on_request is not None:
            state.on_request()
        outcome = state.outcomes.pop(0) if state.outcomes else 200
        if isinstance(outcome, Exception):
            raise outcome
        return Response(outcome)

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(alerts, "urlopen", open_request)
    return state


@pytest.fixture
def alert_markets(postgres_engine):
    rows = []
    with postgres_engine.begin() as connection:
        for index in range(2):
            ticker = f"FIXTURE-ALERT-{uuid4().hex}-{index}"
            market_id = connection.execute(markets.insert().values(
                platform="kalshi", external_id=ticker, title="Synthetic alert market", status="active",
            ).returning(markets.c.id)).scalar_one()
            rows.append({"id": market_id, "ticker": ticker, "title": "Synthetic alert market",
                         "status": "active", "market_type": "binary"})
    yield rows
    ids = [row["id"] for row in rows]
    with postgres_engine.begin() as connection:
        connection.execute(alert_rules.delete().where(alert_rules.c.market_id.in_(ids)))
        connection.execute(snapshots.delete().where(snapshots.c.market_id.in_(ids)))
        for row in rows:
            connection.execute(ingestion_runs.delete().where(
                ingestion_runs.c.watchlist_config.contains({"watchlist": [row["ticker"]]}),
            ))
        connection.execute(markets.delete().where(markets.c.id.in_(ids)))


@pytest.fixture
def alert_client(postgres_engine, monkeypatch):
    monkeypatch.setattr(api, "make_engine", lambda: postgres_engine)
    with TestClient(api.app) as client:
        yield client


def add_rule(engine, market, threshold="0.40", **values):
    with engine.begin() as connection:
        return connection.execute(alert_rules.insert().values(
            market_id=market["id"], threshold=Decimal(threshold), **values,
        ).returning(alert_rules.c.id)).scalar_one()


def rule_state(engine, rule_id):
    with engine.connect() as connection:
        return connection.execute(select(alert_rules).where(alert_rules.c.id == rule_id)).mappings().one()


def make_quote(market, ask, at=BASE):
    levels = [] if ask is None else [[str(Decimal("1") - Decimal(ask)), "2.00"]]
    return normalize_orderbook({"orderbook_fp": {"yes_dollars": [], "no_dollars": levels}}, market["ticker"], at)


def observe(engine, market, ask, minute=0):
    at = BASE + timedelta(minutes=minute)
    saved = save_observation(engine, market, make_quote(market, ask, at), at)
    alerts.process_snapshot_alerts(engine, saved["snapshot_id"])
    return saved


@pytest.mark.parametrize("status, delivered", [(200, True), (204, True), (302, False), (400, False), (429, False), (500, False)])
def test_discord_uses_confirmation_timeout_and_only_successful_statuses(discord_http, status, delivered, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK + "?thread_id=123&wait=false")
    discord_http.outcomes.append(status)
    rule = {"id": 1, "threshold": Decimal("0.40")}
    observation = {"title": "Synthetic alert market", "ticker": "FIXTURE-ALERT",
                   "yes_ask": Decimal("0.39"), "observed_at": BASE}
    assert alerts.send_discord_alert(rule, observation) is delivered
    assert len(discord_http.calls) == 1  # No immediate retries, including 429/500.
    request, timeout = discord_http.calls[0]
    assert request.method == "POST"
    assert parse_qs(urlsplit(request.full_url).query) == {"thread_id": ["123"], "wait": ["true"]}
    assert timeout == 5
    payload = json.loads(request.data)
    assert payload["allowed_mentions"] == {"parse": []}
    for value in ("Synthetic alert market", "FIXTURE-ALERT", "0.40", "0.39", BASE.isoformat()):
        assert value in payload["content"]


def test_no_webhook_does_not_attempt_http(monkeypatch):
    monkeypatch.setattr(alerts, "urlopen", lambda *a, **k: pytest.fail("No webhook means no HTTP"))
    assert alerts.send_discord_alert({}, {}) is False


@pytest.mark.postgres
def test_threshold_sequence_sends_once_rearms_and_sends_again(postgres_engine, alert_markets, discord_http):
    market = alert_markets[0]
    rule_id = add_rule(postgres_engine, market)
    previous_alert_time = None
    for minute, (ask, expected_alerted, sends) in enumerate([
        ("0.43", False, 0), ("0.39", True, 1), ("0.38", True, 1),
        ("0.42", False, 1), ("0.37", True, 2),
    ]):
        observe(postgres_engine, market, ask, minute)
        rule = rule_state(postgres_engine, rule_id)
        assert rule["is_alerted"] is expected_alerted
        assert len(discord_http.calls) == sends
        if sends:
            assert rule["last_alerted_at"].utcoffset() == timedelta(0)
        if minute in (2, 3):
            assert rule["last_alerted_at"] == previous_alert_time
        previous_alert_time = rule["last_alerted_at"]


@pytest.mark.postgres
def test_first_observation_can_trigger_and_equality_rearms(postgres_engine, alert_markets, discord_http):
    market = alert_markets[0]
    rule_id = add_rule(postgres_engine, market)
    observe(postgres_engine, market, "0.39")
    assert rule_state(postgres_engine, rule_id)["is_alerted"] is True
    observe(postgres_engine, market, "0.40", 1)
    assert rule_state(postgres_engine, rule_id)["is_alerted"] is False
    observe(postgres_engine, market, "0.39", 2)
    assert len(discord_http.calls) == 2


@pytest.mark.postgres
def test_missing_ask_and_disabled_rules_do_not_send_or_change_state(postgres_engine, alert_markets, discord_http):
    market = alert_markets[0]
    active = add_rule(postgres_engine, market, is_alerted=True, last_alerted_at=BASE)
    disabled = add_rule(postgres_engine, market, enabled=False)
    observe(postgres_engine, market, None)
    assert rule_state(postgres_engine, active)["is_alerted"] is True
    with postgres_engine.begin() as connection:
        connection.execute(alert_rules.update().where(alert_rules.c.id == active).values(enabled=False))
    observe(postgres_engine, market, "0.39", 1)
    assert rule_state(postgres_engine, disabled)["is_alerted"] is False
    assert discord_http.calls == []


@pytest.mark.postgres
@pytest.mark.parametrize("failure", [
    HTTPError(WEBHOOK, 429, "secret-token", {}, None),
    URLError(WEBHOOK), TimeoutError(WEBHOOK), ValueError(WEBHOOK), 500,
])
def test_discord_failure_remains_eligible_on_later_snapshot(postgres_engine, alert_markets, discord_http, failure, caplog):
    market = alert_markets[0]
    rule_id = add_rule(postgres_engine, market)
    discord_http.outcomes.append(failure)
    observe(postgres_engine, market, "0.39")
    rule = rule_state(postgres_engine, rule_id)
    assert rule["is_alerted"] is False and rule["last_alerted_at"] is None
    assert len(discord_http.calls) == 1
    observe(postgres_engine, market, "0.38", 1)
    assert rule_state(postgres_engine, rule_id)["is_alerted"] is True
    assert len(discord_http.calls) == 2
    assert "Discord notification" in caplog.text
    assert WEBHOOK not in caplog.text and "secret-token" not in caplog.text


@pytest.mark.postgres
@pytest.mark.parametrize("already_alerted", [False, True])
def test_unconfigured_webhook_leaves_alert_state_unchanged(postgres_engine, alert_markets, monkeypatch, already_alerted):
    market = alert_markets[0]
    rule_id = add_rule(postgres_engine, market, is_alerted=already_alerted,
                       last_alerted_at=BASE if already_alerted else None)
    monkeypatch.setattr(alerts, "urlopen", lambda *a, **k: pytest.fail("Unexpected notification"))
    before = dict(rule_state(postgres_engine, rule_id))
    observe(postgres_engine, market, "0.39")
    assert dict(rule_state(postgres_engine, rule_id)) == before


@pytest.mark.postgres
@pytest.mark.parametrize("failure_kind", ["notification", "state_database"])
def test_alert_failure_keeps_committed_snapshots_and_other_markets_successful(
    postgres_engine, alert_markets, discord_http, monkeypatch, caplog, failure_kind,
):
    rule_ids = [add_rule(postgres_engine, market) for market in alert_markets]
    tickers = [market["ticker"] for market in alert_markets]
    by_ticker = {market["ticker"]: market for market in alert_markets}
    monkeypatch.setattr(worker, "fetch_market", lambda client, ticker: by_ticker[ticker])
    monkeypatch.setattr(worker, "fetch_book_quote", lambda client, ticker: make_quote(by_ticker[ticker], "0.39"))

    def check_collection_finished():
        # Even slow delivery cannot consume the budget for the other market.
        with postgres_engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(snapshots).where(
                snapshots.c.market_id.in_([m["id"] for m in alert_markets]),
            )).scalar_one() == 2

    discord_http.on_request = check_collection_finished
    if failure_kind == "notification":
        discord_http.outcomes.append(URLError(WEBHOOK))
    failed_update = []

    def fail_first_alert_update(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE alert_rules") and not failed_update:
            failed_update.append(True)
            raise OperationalError("update", {}, Exception("private-password " + WEBHOOK))

    if failure_kind == "state_database":
        event.listen(postgres_engine, "before_cursor_execute", fail_first_alert_update)
    try:
        counts = worker.collect_cycle(None, postgres_engine, tickers, BASE, 60, Event())
    finally:
        if failure_kind == "state_database":
            event.remove(postgres_engine, "before_cursor_execute", fail_first_alert_update)
    assert counts["inserted"] == 2 and counts["failed"] == 0 and counts["status"] == "success"
    with postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(snapshots).where(
            snapshots.c.market_id.in_([m["id"] for m in alert_markets]),
        )).scalar_one() == 2
    assert all(outcome["outcome"] == "inserted" for outcome in read_run(postgres_engine, counts["run_id"])["market_outcomes"].values())
    assert rule_state(postgres_engine, rule_ids[0])["is_alerted"] is False
    assert rule_state(postgres_engine, rule_ids[1])["is_alerted"] is True
    assert len(discord_http.calls) == 2
    assert ("Discord notification failed" if failure_kind == "notification" else "Alert state database update failed") in caplog.text
    for secret in (WEBHOOK, "secret-token", "private-password"):
        assert secret not in caplog.text


@pytest.mark.postgres
def test_worker_does_not_alert_on_uncommitted_duplicate_values(postgres_engine, alert_markets, discord_http, monkeypatch):
    market = alert_markets[0]
    add_rule(postgres_engine, market)
    monkeypatch.setattr(worker, "fetch_market", lambda client, ticker: market)
    ask = ["0.43"]
    monkeypatch.setattr(worker, "fetch_book_quote", lambda client, ticker: make_quote(market, ask[0]))
    first = worker.collect_cycle(None, postgres_engine, [market["ticker"]], BASE, 60, Event())
    ask[0] = "0.39"
    duplicate = worker.collect_cycle(None, postgres_engine, [market["ticker"]], BASE, 60, Event())
    assert first["inserted"] == 1 and duplicate["duplicate"] == 1
    assert discord_http.calls == []
    worker.collect_cycle(None, postgres_engine, [market["ticker"]], BASE + timedelta(minutes=1), 60, Event())
    assert len(discord_http.calls) == 1


@pytest.mark.postgres
def test_unexpected_alert_failure_does_not_escape_the_worker(postgres_engine, alert_markets, monkeypatch, caplog):
    tickers = [market["ticker"] for market in alert_markets]
    by_ticker = {market["ticker"]: market for market in alert_markets}
    monkeypatch.setattr(worker, "fetch_market", lambda client, ticker: by_ticker[ticker])
    monkeypatch.setattr(worker, "fetch_book_quote", lambda client, ticker: make_quote(by_ticker[ticker], "0.39"))
    attempts = []

    def fail_processing(engine, snapshot_id):
        attempts.append(snapshot_id)
        raise RuntimeError(WEBHOOK)

    monkeypatch.setattr(worker, "process_snapshot_alerts", fail_processing)
    counts = worker.collect_cycle(None, postgres_engine, tickers, BASE, 60, Event())
    assert counts["inserted"] == 2 and counts["failed"] == 0 and counts["status"] == "success"
    assert len(attempts) == 2
    assert "Alert processing failed" in caplog.text and "secret-token" not in caplog.text


@pytest.mark.postgres
def test_api_create_list_disable_and_preserve_decimal(postgres_engine, alert_markets, alert_client, discord_http):
    market = alert_markets[0]
    threshold = "0.40000000000000000001"
    response = alert_client.post("/alert-rules", json={"market_id": market["id"], "threshold": threshold})
    assert response.status_code == 201
    rule = response.json()
    assert rule == {"id": rule["id"], "market_id": market["id"], "threshold": threshold,
                    "enabled": True, "is_alerted": False, "last_alerted_at": None}
    assert rule_state(postgres_engine, rule["id"])["threshold"] == Decimal(threshold)
    second = alert_client.post("/alert-rules", json={"market_id": market["id"], "threshold": "0.30"}).json()
    listed = alert_client.get("/alert-rules?limit=100").json()
    assert [row["id"] for row in listed] == sorted(row["id"] for row in listed)
    offset = next(index for index, row in enumerate(listed) if row["id"] == rule["id"])
    assert alert_client.get(f"/alert-rules?limit=1&offset={offset}").json() == [rule]
    assert alert_client.get(f"/alert-rules?limit=1&offset={offset+1}").json() == [second]
    assert alert_client.get("/alert-rules?offset=9223372036854775807").json() == []
    observe(postgres_engine, market, "0.40")
    assert rule_state(postgres_engine, rule["id"])["is_alerted"] is True
    assert len(discord_http.calls) == 1  # Decimal precision is not rounded to 0.40.
    # Exact comparison would trigger at ask 0.40; disabling prevents processing.
    for _ in range(2):
        disabled = alert_client.post(f"/alert-rules/{rule['id']}/disable")
        assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    observe(postgres_engine, market, "0.40", 1)
    assert len(discord_http.calls) == 1


@pytest.mark.postgres
def test_delivery_reads_a_snapshot_visible_on_another_connection(postgres_engine, alert_markets, discord_http, monkeypatch):
    market = alert_markets[0]
    rule_id = add_rule(postgres_engine, market)
    monkeypatch.setattr(worker, "fetch_market", lambda client, ticker: market)
    monkeypatch.setattr(worker, "fetch_book_quote", lambda client, ticker: make_quote(market, "0.39"))

    def check_commit():
        with postgres_engine.connect() as connection:
            assert connection.execute(select(snapshots.c.yes_ask).where(
                snapshots.c.market_id == market["id"],
            )).scalar_one() == Decimal("0.39")

    discord_http.on_request = check_commit
    counts = worker.collect_cycle(None, postgres_engine, [market["ticker"]], BASE, 60, Event())
    assert counts["status"] == "success" and len(discord_http.calls) == 1
    assert rule_state(postgres_engine, rule_id)["is_alerted"] is True


@pytest.mark.postgres
def test_alert_read_database_failure_does_not_change_ingestion(postgres_engine, alert_markets, discord_http, monkeypatch, caplog):
    market = alert_markets[0]
    add_rule(postgres_engine, market)
    monkeypatch.setattr(worker, "fetch_market", lambda client, ticker: market)
    monkeypatch.setattr(worker, "fetch_book_quote", lambda client, ticker: make_quote(market, "0.39"))

    def fail_alert_read(connection, cursor, statement, parameters, context, executemany):
        if "markets.external_id AS ticker" in statement:
            raise OperationalError("select", {}, Exception("private-password " + WEBHOOK))

    event.listen(postgres_engine, "before_cursor_execute", fail_alert_read)
    try:
        counts = worker.collect_cycle(None, postgres_engine, [market["ticker"]], BASE, 60, Event())
    finally:
        event.remove(postgres_engine, "before_cursor_execute", fail_alert_read)
    assert counts["inserted"] == 1 and counts["failed"] == 0 and counts["status"] == "success"
    assert discord_http.calls == []
    assert "Alert database read failed" in caplog.text
    assert "secret-token" not in caplog.text and "private-password" not in caplog.text


@pytest.mark.postgres
def test_api_write_failure_uses_safe_database_error(postgres_engine, alert_markets, alert_client):
    def fail_alert_write(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO alert_rules"):
            raise OperationalError("insert", {}, Exception("private-password"))

    event.listen(postgres_engine, "before_cursor_execute", fail_alert_write)
    try:
        response = alert_client.post("/alert-rules", json={"market_id": alert_markets[0]["id"], "threshold": "0.40"})
    finally:
        event.remove(postgres_engine, "before_cursor_execute", fail_alert_write)
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    with postgres_engine.connect() as connection:
        assert connection.execute(select(alert_rules.c.id).where(
            alert_rules.c.market_id == alert_markets[0]["id"],
        )).first() is None


@pytest.mark.parametrize("threshold", ["0", "1", "-0.01", "1.01", "NaN", "Infinity", "-Infinity", "oops", None, True])
def test_api_rejects_invalid_threshold_before_queries(threshold):
    api.app.dependency_overrides[api.get_connection] = lambda: None
    try:
        with TestClient(api.app) as client:
            assert client.post("/alert-rules", json={"market_id": 1, "threshold": threshold}).status_code == 422
    finally:
        api.app.dependency_overrides.clear()


@pytest.mark.parametrize("path", [
    "/alert-rules?limit=0", "/alert-rules?limit=101", "/alert-rules?offset=-1",
    "/alert-rules?offset=9223372036854775808", "/alert-rules?limit=wrong",
])
def test_api_rejects_unbounded_or_invalid_pagination(path):
    api.app.dependency_overrides[api.get_connection] = lambda: None
    try:
        with TestClient(api.app) as client:
            assert client.get(path).status_code == 422
    finally:
        api.app.dependency_overrides.clear()


@pytest.mark.postgres
def test_api_rejects_unknown_market_and_rule(alert_client):
    response = alert_client.post("/alert-rules", json={"market_id": 9223372036854775807, "threshold": "0.40"})
    assert response.status_code == 404 and response.json() == {"detail": "Market not found"}
    response = alert_client.post("/alert-rules/9223372036854775807/disable")
    assert response.status_code == 404 and response.json() == {"detail": "Alert rule not found"}


@pytest.mark.postgres
@pytest.mark.parametrize("threshold", ["0", "1", "-0.01", "1.01", "NaN", "Infinity", "-Infinity"])
def test_database_rejects_invalid_thresholds(postgres_engine, alert_markets, threshold):
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        try:
            with pytest.raises(IntegrityError) as error:
                connection.execute(alert_rules.insert().values(market_id=alert_markets[0]["id"], threshold=Decimal(threshold)))
            assert error.value.orig.diag.constraint_name == "ck_alert_rules_threshold"
        finally:
            transaction.rollback()

"""Bounded GET retry behavior with synthetic HTTP and fake time."""

from datetime import timedelta
from email.utils import format_datetime
import math

import httpx
import pytest

from fake_time import FakeTime
from fetch_raw import BASE_URL, TIMEOUT, fetch_json
from fetch_quote import fetch_market
from retrying import BudgetExhausted, CollectionStopped, CycleBudget, RetryingClient, retry_after_seconds


def requester(client, clock, *, seconds=50, retry_seconds=10, budget=None):
    budget = budget or CycleBudget(seconds, retry_seconds, monotonic=clock.monotonic)
    return RetryingClient(client, budget, clock, 1, "FIXTURE", now=clock.now, uniform=lambda low, high: high)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_temporary_status_retries_then_succeeds(status):
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request)
        return httpx.Response(status if len(calls) < 3 else 200, json={"ok": True})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        retry = requester(client, clock)
        assert fetch_json(retry, "markets/FIXTURE") == {"ok": True}
    assert len(calls) == 3 and retry.attempts["metadata"] == 3
    assert clock.waits == [0.5, 1.0]


@pytest.mark.parametrize("error_class", [httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.ReadError])
def test_temporary_transport_errors_retry(error_class):
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise error_class("Synthetic network failure", request=request)
        return httpx.Response(200, json={})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        assert fetch_json(requester(client, clock), "markets/FIXTURE") == {}
    assert len(calls) == 3 and clock.waits == [0.5, 1.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 501])
def test_permanent_status_is_not_retried(status):
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_json(requester(client, clock), "markets/FIXTURE")
    assert len(calls) == 1 and clock.waits == []


def test_temporary_failure_stops_after_three_attempts():
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_json(requester(client, clock), "markets/FIXTURE")
    assert len(calls) == 3 and clock.waits == [0.5, 1.0]


@pytest.mark.parametrize("body", ["not JSON", '{"market": null}'])
def test_invalid_response_data_is_not_retried(body):
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=body)

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            fetch_market(requester(client, clock), "FIXTURE")
    assert len(calls) == 1 and clock.waits == []


@pytest.mark.parametrize("date_header", [False, True])
def test_retry_after_seconds_and_http_date_are_respected(date_header):
    clock, calls = FakeTime(), []
    value = format_datetime(clock.now() + timedelta(seconds=3), usegmt=True) if date_header else "3"

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": value}) if len(calls) == 1 else httpx.Response(200, json={})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        assert fetch_json(requester(client, clock), "markets/FIXTURE") == {}
    assert clock.waits == [3] and len(calls) == 2


@pytest.mark.parametrize("value", [None, "", "bad", "-1", "1.5", "nan", "Wed, 14 Oct 2026 12:00:00"])
def test_malformed_retry_after_is_ignored(value):
    assert retry_after_seconds(value, FakeTime().now()) is None


def test_past_and_huge_retry_after_values():
    clock = FakeTime()
    assert retry_after_seconds(format_datetime(clock.now() - timedelta(seconds=3), usegmt=True), clock.now()) == 0
    assert retry_after_seconds("9" * 500, clock.now()) == math.inf


@pytest.mark.parametrize("seconds, retry_seconds", [(2, 10), (50, 2)])
def test_server_delay_that_cannot_fit_stops_cycle_without_early_requests(seconds, retry_seconds):
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3"})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        retry = requester(client, clock, seconds=seconds, retry_seconds=retry_seconds)
        with pytest.raises(BudgetExhausted, match="retry_after_exceeds_budget"):
            fetch_json(retry, "markets/FIXTURE")
        with pytest.raises(BudgetExhausted):
            fetch_json(requester(client, clock, budget=retry.budget), "markets/OTHER")
    assert len(calls) == 1 and clock.waits == []


def test_retry_after_on_final_attempt_prevents_more_early_cycle_requests():
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        retry = requester(client, clock)
        with pytest.raises(httpx.HTTPStatusError):
            fetch_json(retry, "markets/FIXTURE")
        with pytest.raises(BudgetExhausted):
            fetch_json(requester(client, clock, budget=retry.budget), "markets/OTHER")
    assert len(calls) == 3 and clock.waits == [1, 1]


def test_retry_wait_budget_is_shared_without_blocking_initial_collection_requests():
    clock, calls = FakeTime(), []
    counts = {}

    def handler(request):
        calls.append(request)
        path = request.url.path
        counts[path] = counts.get(path, 0) + 1
        return httpx.Response(503 if counts[path] == 1 and not path.endswith("/C") else 200, json={})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        retry = requester(client, clock, retry_seconds=0.75)
        fetch_json(retry, "markets/A")
        with pytest.raises(BudgetExhausted, match="retry_wait_budget_exhausted"):
            fetch_json(retry, "markets/B")
        assert fetch_json(retry, "markets/C") == {}
    assert len(calls) == 4 and clock.waits == [0.5]


def test_budget_cannot_cancel_an_inflight_request_but_blocks_the_next_one():
    clock, calls = FakeTime(), []

    def handler(request):
        calls.append(request)
        clock.advance(15)  # MockTransport deliberately does not implement network timeouts.
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        retry = requester(client, clock, seconds=2)
        assert fetch_json(retry, "markets/A") == {"ok": True}
        with pytest.raises(BudgetExhausted, match="cycle_budget_exhausted"):
            fetch_json(retry, "markets/B")
    assert len(calls) == 1
    assert calls[0].extensions["timeout"]["read"] == 2
    assert calls[0].extensions["timeout"]["connect"] == 2


def test_shutdown_wakes_retry_wait_and_prevents_another_attempt():
    clock, calls = FakeTime(), []
    clock.stop_during_wait = True

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT, transport=httpx.MockTransport(handler)) as client:
        retry = requester(client, clock)
        with pytest.raises(CollectionStopped):
            fetch_json(retry, "markets/A")
    assert len(calls) == 1 and retry.budget.waited == 0.1

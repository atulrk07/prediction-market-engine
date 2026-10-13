"""Synthetic pages and mocked HTTP; no exchange access required."""

import logging

import httpx
import pytest

from discover_markets import discover_markets
from fetch_raw import BASE_URL, TIMEOUT, fetch_json


def market(ticker, status="active", market_type="binary"):
    return {"ticker": ticker, "title": f"Synthetic market {ticker}",
            "status": status, "market_type": market_type}


def mock_client(handler):
    return httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT,
                        transport=httpx.MockTransport(handler))


def test_follows_cursor_and_requests_only_remaining_count():
    requests = []
    pages = [
        {"markets": [market("FIXTURE-A"), market("FIXTURE-B")], "cursor": "fixture-next"},
        {"markets": [market("FIXTURE-C")], "cursor": "fixture-more"},
    ]

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    with mock_client(handler) as client:
        result = discover_markets(client, limit=3, series_ticker="FIXTURE-SERIES", page_size=2)
    assert [m["ticker"] for m in result] == ["FIXTURE-A", "FIXTURE-B", "FIXTURE-C"]
    assert len(requests) == 2  # No third page despite its available cursor.
    assert requests[0].url.params["status"] == "open"
    assert requests[0].url.params["mve_filter"] == "exclude"
    assert requests[0].url.params["series_ticker"] == "FIXTURE-SERIES"
    assert "cursor" not in requests[0].url.params
    assert requests[0].url.params["limit"] == "2"
    assert requests[1].url.params["cursor"] == "fixture-next"
    assert requests[1].url.params["limit"] == "1"


def test_filtered_and_duplicate_records_do_not_fill_the_requested_limit():
    pages = [
        {"markets": [market("FIXTURE-CLOSED", status="closed"), market("FIXTURE-OTHER", market_type="other"), market("FIXTURE-A")], "cursor": "fixture-next"},
        {"markets": [market("FIXTURE-A"), market("FIXTURE-B")], "cursor": ""},
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    with mock_client(handler) as client:
        result = discover_markets(client, limit=2)
    assert [m["ticker"] for m in result] == ["FIXTURE-A", "FIXTURE-B"]
    assert len(requests) == 2


@pytest.mark.parametrize("cursor", [None, ""])
def test_empty_final_page_returns_fewer_candidates(cursor):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"markets": [], "cursor": cursor})

    with mock_client(handler) as client:
        assert discover_markets(client, limit=3) == []
    assert len(requests) == 1


def test_page_cap_returns_partial_list_with_warning(caplog):
    requests = []

    def handler(request):
        requests.append(request)
        index = len(requests)
        return httpx.Response(200, json={"markets": [market(f"FIXTURE-{index}")], "cursor": f"fixture-next-{index}"})

    with caplog.at_level(logging.WARNING), mock_client(handler) as client:
        result = discover_markets(client, limit=3, page_size=1, max_pages=2)
    assert len(result) == 2
    assert len(requests) == 2
    assert "Page cap reached" in caplog.text


def test_repeated_cursor_fails_instead_of_looping():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"markets": [], "cursor": "fixture-repeat"})

    with mock_client(handler) as client, pytest.raises(ValueError, match="Repeated pagination cursor"):
        discover_markets(client, limit=3)
    assert len(requests) == 2


@pytest.mark.parametrize("options", [
    {"limit": 0}, {"limit": 101}, {"limit": -1}, {"limit": True},
    {"page_size": 0}, {"page_size": 101}, {"max_pages": 0},
    {"max_pages": 11}, {"series_ticker": " "},
])
def test_invalid_limits_fail_before_any_request(options):
    def handler(request):
        pytest.fail("Invalid input must not request Kalshi")

    with mock_client(handler) as client, pytest.raises(ValueError):
        discover_markets(client, **options)


@pytest.mark.parametrize("data,reason", [
    ({}, "markets array"),
    ({"markets": None, "cursor": ""}, "markets array"),
    ({"markets": []}, "Missing pagination cursor"),
    ({"markets": [], "cursor": 7}, "pagination cursor"),
    ({"markets": [None], "cursor": ""}, "market to be an object"),
    ({"markets": [{"ticker": "FIXTURE-A"}], "cursor": ""}, "invalid title"),
])
def test_malformed_pages_fail_clearly(data, reason):
    with mock_client(lambda request: httpx.Response(200, json=data)) as client:
        with pytest.raises(ValueError, match=reason):
            discover_markets(client)


@pytest.mark.parametrize("status", [404, 429, 503])
def test_http_errors_propagate_without_retries(status):
    requests = []

    def handler(request):
        requests.append(request)
        # Invalid JSON: HTTP status must be checked before JSON decoding.
        return httpx.Response(status, text="upstream failure")

    with mock_client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        discover_markets(client)
    assert len(requests) == 1


def test_explicit_timeouts_and_timeout_failure():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.extensions["timeout"] == {"connect": 5.0, "read": 10.0, "write": 10.0, "pool": 10.0}
        raise httpx.ReadTimeout("Synthetic timeout", request=request)

    with mock_client(handler) as client, pytest.raises(httpx.ReadTimeout):
        discover_markets(client)
    assert len(requests) == 1


def test_http_helper_rejects_non_object_json():
    with mock_client(lambda request: httpx.Response(200, json=[])) as client:
        with pytest.raises(ValueError, match="JSON object"):
            fetch_json(client, "markets")

"""Verify command errors and honest fixture labels without real HTTP."""

import json
from pathlib import Path
import sys

import httpx
import pytest

import discover_markets
import fetch_quote
import ingest_quote
from fetch_raw import BASE_URL, TIMEOUT


def test_fixture_command_has_no_http_calls(tmp_path, monkeypatch, capsys):
    fixture = Path(__file__).parent / "fixtures" / "orderbooks.json"
    path = tmp_path / "two_sided.json"
    path.write_text(json.dumps(json.loads(fixture.read_text())["two_sided"]))
    monkeypatch.setattr(sys, "argv", ["fetch_quote.py", "FIXTURE-EXAMPLE", "--fixture", str(path)])
    assert fetch_quote.main() == 0
    output = capsys.readouterr().out
    assert output.startswith("FIXTURE quote (synthetic data")
    assert '"ticker": "FIXTURE-EXAMPLE"' in output


def test_malformed_fixture_exits_one_without_a_quote(tmp_path, monkeypatch, capsys, caplog):
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps({"orderbook_fp": {}}))
    monkeypatch.setattr(sys, "argv", ["fetch_quote.py", "FIXTURE-INVALID", "--fixture", str(path)])
    assert fetch_quote.main() == 1
    assert capsys.readouterr().out == ""
    assert "missing required yes_dollars" in caplog.text


def test_discovery_timeout_exits_one_without_candidates(monkeypatch, capsys, caplog):
    def handler(request):
        raise httpx.ReadTimeout("Synthetic timeout", request=request)

    client = httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT,
                          transport=httpx.MockTransport(handler))
    monkeypatch.setattr(discover_markets.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(sys, "argv", ["discover_markets.py", "--limit", "3"])
    assert discover_markets.main() == 1
    assert capsys.readouterr().out == ""
    assert "Discovery failed" in caplog.text


def test_ingestion_http_failure_does_not_open_database(monkeypatch, capsys, caplog):
    def handler(request):
        return httpx.Response(503, text="Synthetic upstream outage")

    client = httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT,
                          transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ingest_quote.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(ingest_quote, "make_engine", lambda: pytest.fail("Failed HTTP must not save a quote"))
    monkeypatch.setattr(sys, "argv", ["ingest_quote.py", "FIXTURE-FAILED"])
    assert ingest_quote.main() == 1
    assert capsys.readouterr().out == ""
    assert "Collection failed" in caplog.text

"""Measurement reports must match API pagination and avoid overstating coverage."""

import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest

import bench

spec = importlib.util.spec_from_file_location("collection_coverage", Path(__file__).parents[1] / "coverage.py")
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)


def test_coverage_reads_all_market_and_history_pages():
    start = coverage.parse_iso("2026-10-14T00:00:00Z")
    end = coverage.parse_iso("2026-10-15T00:00:00Z")
    offsets = []

    def handler(request):
        offset = int(request.url.params["offset"])
        if request.url.path == "/markets":
            return httpx.Response(200, json=[{"id": n} for n in range(offset, min(offset + 100, 101))])
        assert request.url.params["start"] == start.isoformat()
        assert request.url.params["end"] == end.isoformat()
        offsets.append(offset)
        count = 1000 if offset == 0 else 1
        return httpx.Response(200, json={"observations": [
            {"quote": {"observed_at": "2026-10-14T00:00:00Z"}} for _ in range(count)
        ]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(coverage.list_markets(client, "http://test")) == 101
        assert len(coverage.fetch_history(client, "http://test", 1, start, end)) == 1001
    assert offsets == [0, 1000]


def test_slot_coverage_deduplicates_and_handles_partial_slot_and_empty_market():
    start = coverage.parse_iso("2026-10-14T00:00:00Z")
    end = coverage.parse_iso("2026-10-14T00:01:30Z")
    rows = [{"quote": {"observed_at": time}} for time in [
        "2026-10-13T23:59:59Z", "2026-10-14T00:00:00Z",
        "2026-10-14T00:00:10Z", "2026-10-14T00:01:00Z", "2026-10-14T00:01:30Z",
    ]]
    report = coverage.build_report({1: rows, 2: []}, start, end, 60)
    assert report["total_observations"] == 3
    assert report["expected_slots_per_market"] == 2
    assert report["per_market"][1]["coverage_pct"] == 100
    assert report["per_market"][2]["coverage_pct"] == 0
    assert report["overall_slot_coverage_pct"] == 50


def test_coverage_http_failures_are_not_reported_as_empty_history():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            coverage.fetch_history(client, "http://test", 1,
                                   coverage.parse_iso("2026-10-14T00:00:00Z"),
                                   coverage.parse_iso("2026-10-15T00:00:00Z"))


def test_coverage_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone"):
        coverage.parse_iso("2026-10-14T00:00:00")


def test_benchmark_picks_database_id_instead_of_ticker():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json=[{"id": 7, "ticker": "KXHIGHNY-EXAMPLE"}],
    ))) as client:
        assert bench.pick_market(client, "http://test", None) == 7


def test_markets_benchmark_runs_without_stored_markets(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["bench.py", "--endpoint", "markets", "--requests", "2",
                                     "--concurrency", "1", "--warmup", "0", "--out", str(report_path)])
    results = iter([("ok", 5), ("http_503", 10)])
    monkeypatch.setattr(bench, "one_request", lambda *args: next(results))
    monkeypatch.setattr(bench, "pick_market", lambda *args: pytest.fail("Markets endpoint needs no ID"))
    bench.main()
    report = json.loads(report_path.read_text())
    assert report["ok"] == 1
    assert report["errors"] == 1
    assert report["error_breakdown"] == {"http_503": 1}
    assert report["latency_ms"]["p95"] == 5


@pytest.mark.parametrize("arguments", [["--requests", "0"], ["--concurrency", "0"],
                                       ["--warmup", "-1"], ["--timeout", "nan"], ["--market", "0"]])
def test_benchmark_rejects_invalid_arguments_before_http(arguments, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bench.py", *arguments])
    with pytest.raises(SystemExit) as error:
        bench.main()
    assert error.value.code == 2

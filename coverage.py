#!/usr/bin/env python3
"""Measure occupied observation slots through the public API.

Usage:
    python coverage.py --interval 60 --start 2026-10-14T00:00:00Z --end 2026-10-15T00:00:00Z

Defaults to a fixed last-24-hours window, with at most seven days per report.
Slots begin at the window start; a final partial slot counts as one expected slot.
Counts use quote.observed_at, include start, and exclude end. All stored markets
are included, even those without observations. Choose a window in which those
markets were intended to be tracked. Occupied slots do not establish continuous
uptime or on-time delivery; late observations can still occupy a slot.
"""

import argparse
import datetime as dt
import json
import math

import httpx

from history import history_range


def parse_iso(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamps must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def list_markets(client, base_url):
    ids = []
    offset = 0
    while True:
        response = client.get(f"{base_url}/markets", params={"limit": 100, "offset": offset})
        response.raise_for_status()
        rows = response.json()
        ids.extend(row["id"] for row in rows)
        if len(rows) < 100:
            return ids
        offset += len(rows)


def fetch_history(client, base_url, market, start, end):
    observations = []
    offset = 0
    while True:
        response = client.get(f"{base_url}/markets/{market}/history", params={
            "limit": 1000, "offset": offset,
            "start": start.isoformat(), "end": end.isoformat(),
        })
        response.raise_for_status()
        rows = response.json()["observations"]
        observations.extend(rows)
        if len(rows) < 1000:
            return observations
        offset += len(rows)


def build_report(per_market_rows, start, end, interval):
    start, end = history_range(start, end, dt.datetime.now(dt.timezone.utc))
    if interval <= 0:
        raise ValueError("Interval must be positive")
    window_seconds = (end - start).total_seconds()
    expected = math.ceil(window_seconds / interval)
    per_market = {}
    for market, rows in per_market_rows.items():
        times = [parse_iso(row["quote"]["observed_at"]) for row in rows]
        times = [time for time in times if start <= time < end]
        covered = {int((time - start).total_seconds() // interval) for time in times}
        per_market[market] = {
            "observations": len(times), "covered_slots": len(covered),
            "expected_slots": expected, "coverage_pct": round(100 * len(covered) / expected, 2),
        }
    if not per_market:
        raise ValueError("No markets found. Collect some data first.")
    total_covered = sum(row["covered_slots"] for row in per_market.values())
    return {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "interval_seconds": interval, "time_field_used": "quote.observed_at",
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "window_hours": round(window_seconds / 3600, 2),
        "markets": len(per_market), "expected_slots_per_market": expected,
        "total_observations": sum(row["observations"] for row in per_market.values()),
        "overall_slot_coverage_pct": round(100 * total_covered / (expected * len(per_market)), 2),
        "per_market": per_market,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--start", type=parse_iso)
    parser.add_argument("--end", type=parse_iso)
    parser.add_argument("--out", default="coverage_report.json")
    args = parser.parse_args()
    try:
        start, end = history_range(args.start, args.end, dt.datetime.now(dt.timezone.utc))
        if args.interval <= 0:
            raise ValueError("Interval must be positive")
    except ValueError as error:
        parser.error(str(error))
    with httpx.Client(timeout=30) as client:
        markets = list_markets(client, args.base_url.rstrip("/"))
        rows = {market: fetch_history(client, args.base_url.rstrip("/"), market, start, end)
                for market in markets}
    report = build_report(rows, start, end, args.interval)
    report["base_url"] = args.base_url
    with open(args.out, "w") as output:
        json.dump(report, output, indent=2)
    print(json.dumps(report, indent=2))
    print(f"Saved {args.out}")


if __name__ == "__main__":
    main()

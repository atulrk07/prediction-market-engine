#!/usr/bin/env python3
"""
bench.py — small, honest latency benchmark for the prediction-market-engine API.

What it does:
  Warms up, then sends a fixed number of requests to one endpoint at a chosen
  concurrency level, and reports p50/p95/p99 latency, throughput, and error/timeout
  counts. Saves aggregate results and run metadata to a JSON file.
  Latency includes creating and closing a new HTTPX client/connection per request;
  percentiles include successful responses only. This is not server-only latency.

What it does NOT do:
  It does not ingest or modify data. Point it at a running API that already has a
  fixed DB snapshot loaded. Do NOT mix benchmark traffic with a live collection run.

Usage:
  # history endpoint, 2000 requests, 20 concurrent clients
  python bench.py --endpoint history --requests 2000 --concurrency 20

  # quote endpoint
  python bench.py --endpoint quote --requests 2000 --concurrency 20

  # run a sweep of concurrency levels
  for c in 1 5 10 20 50; do python bench.py --endpoint history --concurrency $c --requests 2000; done

Verify before trusting numbers:
  - --base-url matches your running API (default http://localhost:8000).
  - The script auto-picks a database ID from GET /markets; pass --market ID to pin one.
  - Record your hardware (CPU/RAM) — the script asks you to pass --env for the report.
"""
import argparse, json, math, statistics, sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import httpx
except ImportError:
    sys.exit("httpx not installed. Run: pip install httpx")


def percentile(values, p):
    """Linear-interpolation percentile. p in [0,100]."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def pick_market(client, base_url, market):
    if market:
        return market
    r = client.get(f"{base_url}/markets", params={"limit": 1})
    r.raise_for_status()
    data = r.json()
    # Be tolerant about response shape: list, or {"markets": [...]}, or {"items": [...]}
    rows = data if isinstance(data, list) else data.get("markets") or data.get("items") or []
    if not rows:
        sys.exit("No markets found. Collect some data first, or pass --market ID.")
    first = rows[0]
    # Try common id field names
    for key in ("id", "market_id"):
        if isinstance(first, dict) and key in first:
            return first[key]
    sys.exit(f"Could not find a market id in {first!r}. Pass --market ID explicitly.")


def build_url(base_url, endpoint, market):
    if endpoint == "history":
        return f"{base_url}/markets/{market}/history", {"limit": 100}
    if endpoint == "quote":
        return f"{base_url}/markets/{market}/quote", {}
    if endpoint == "markets":
        return f"{base_url}/markets", {"limit": 100}
    sys.exit(f"Unknown endpoint: {endpoint}")


def one_request(url, params, timeout):
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(url, params=params)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return ("ok" if r.status_code == 200 else f"http_{r.status_code}", elapsed_ms)
    except httpx.TimeoutException:
        return ("timeout", (time.perf_counter() - t0) * 1000.0)
    except Exception as e:  # noqa: BLE001
        return (f"error:{type(e).__name__}", (time.perf_counter() - t0) * 1000.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--endpoint", choices=["history", "quote", "markets"], default="history")
    ap.add_argument("--market", type=int, default=None, help="Database market ID; auto-picked if omitted")
    ap.add_argument("--requests", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=100)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--env", default="unspecified-hardware",
                    help="Free text, e.g. 'M2 Air, 16GB, Docker'. Goes in the report.")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if args.requests <= 0 or args.concurrency <= 0 or args.warmup < 0:
        ap.error("Requests and concurrency must be positive; warmup must be nonnegative")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        ap.error("Timeout must be finite and positive")
    if args.market is not None and args.market <= 0:
        ap.error("Market ID must be positive")
    args.base_url = args.base_url.rstrip("/")

    market = None
    if args.endpoint != "markets":
        with httpx.Client(timeout=args.timeout) as c:
            market = pick_market(c, args.base_url, args.market)
    url, params = build_url(args.base_url, args.endpoint, market)
    print(f"Benchmarking {url}  params={params}")
    print(f"warmup={args.warmup}  requests={args.requests}  concurrency={args.concurrency}")

    # Warmup (not measured)
    for _ in range(args.warmup):
        one_request(url, params, args.timeout)

    # Measured run
    latencies, statuses = [], []
    wall0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(one_request, url, params, args.timeout)
                   for _ in range(args.requests)]
        for f in as_completed(futures):
            status, ms = f.result()
            statuses.append(status)
            if status == "ok":
                latencies.append(ms)
    wall = time.perf_counter() - wall0

    ok = len(latencies)
    errors = len(statuses) - ok
    throughput = ok / wall if wall > 0 else 0.0

    report = {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base_url": args.base_url,
        "endpoint": args.endpoint,
        "market": market,
        "params": params,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "warmup": args.warmup,
        "environment": args.env,
        "latency_scope": "Successful responses; includes per-request HTTPX client and connection setup/teardown",
        "ok": ok,
        "errors": errors,
        "error_breakdown": {s: statuses.count(s) for s in sorted(set(statuses)) if s != "ok"},
        "wall_seconds": round(wall, 3),
        "throughput_rps": round(throughput, 1),
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 1) if latencies else None,
            "p95": round(percentile(latencies, 95), 1) if latencies else None,
            "p99": round(percentile(latencies, 99), 1) if latencies else None,
            "min": round(min(latencies), 1) if latencies else None,
            "max": round(max(latencies), 1) if latencies else None,
            "mean": round(statistics.fmean(latencies), 1) if latencies else None,
        },
    }

    print("\n=== RESULT ===")
    print(json.dumps(report, indent=2))

    out = args.out or f"bench_{args.endpoint}_c{args.concurrency}.json"
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved {out}")
    if errors:
        print(f"NOTE: {errors} non-200 responses — report errors separately, do not hide them.")


if __name__ == "__main__":
    main()

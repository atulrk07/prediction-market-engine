"""Inspect raw Kalshi market and order-book JSON."""

import argparse
import json
import sys
from urllib.parse import quote

import httpx


BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def fetch_json(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    """Fetch once; reject HTTP errors before decoding JSON."""
    response = client.get(path, params=params)
    print(f"GET {response.url} -> HTTP {response.status_code}", file=sys.stderr)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object at the response root")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="Exact Kalshi market ticker")
    args = parser.parse_args()
    ticker = quote(args.ticker, safe="")

    try:
        with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT) as client:
            market = fetch_json(client, f"markets/{ticker}")
            print("LIVE market response:", flush=True)
            print(json.dumps(market, indent=2), flush=True)
            orderbook = fetch_json(client, f"markets/{ticker}/orderbook")
            print("LIVE order-book response:", flush=True)
            print(json.dumps(orderbook, indent=2), flush=True)
    except (httpx.HTTPError, ValueError) as error:
        print(f"Fetch failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

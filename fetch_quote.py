"""Step 1B: print a normalized live quote or an explicitly labeled fixture."""

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from urllib.parse import quote

import httpx

from fetch_raw import BASE_URL, TIMEOUT, fetch_json
from normalize_quote import Quote, normalize_orderbook


def fetch_live_quote(client: httpx.Client, ticker: str) -> tuple[dict, Quote]:
    """Fetch metadata and a book once, then return metadata and a valid quote."""
    ticker_path = quote(ticker, safe="")
    metadata = fetch_json(client, f"markets/{ticker_path}")
    market = metadata.get("market")
    if not isinstance(market, dict):
        raise ValueError("Expected a market object")
    if market.get("ticker") != ticker or market.get("market_type") != "binary":
        raise ValueError("Expected the requested binary market")
    if not isinstance(market.get("status"), str) or not market["status"]:
        raise ValueError("Expected a market status string")
    print(f"Market status: {market['status']}", file=sys.stderr)
    data = fetch_json(client, f"markets/{ticker_path}/orderbook")
    observed_at = datetime.now(timezone.utc)
    return market, normalize_orderbook(data, ticker, observed_at)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="Exact Kalshi ticker, or a fixture identifier")
    parser.add_argument("--fixture", type=Path, help="Read a synthetic JSON book; no HTTP calls")
    args = parser.parse_args()

    try:
        if args.fixture is not None:
            data = json.loads(args.fixture.read_text())
            observed_at = datetime.now(timezone.utc)
            result = normalize_orderbook(data, args.ticker, observed_at)
            label = "FIXTURE quote (synthetic data; timestamp is local inspection time)"
        else:
            with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT) as client:
                _, result = fetch_live_quote(client, args.ticker)
            label = "LIVE normalized quote"

        if result.is_crossed:
            logging.warning("%s: crossed quote; preserving observed values", args.ticker)
        print(f"{label}:")
        print(result.model_dump_json(indent=2))
    except (httpx.HTTPError, ValueError, OSError) as error:
        logging.error("Quote failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

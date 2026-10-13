"""Step 1C: list a bounded number of live active binary-market candidates."""

import argparse
import logging
import sys

import httpx

from fetch_raw import BASE_URL, TIMEOUT, fetch_json


DEFAULT_PAGE_SIZE = 25
DEFAULT_MAX_PAGES = 5


def discover_markets(
    client: httpx.Client,
    limit: int = 10,
    series_ticker: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[dict]:
    """Follow cursors until enough candidates, exhaustion, or the page cap."""
    for name, value, maximum in (
        ("limit", limit, 100),
        ("page_size", page_size, 100),
        ("max_pages", max_pages, 10),
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    if series_ticker is not None and not series_ticker.strip():
        raise ValueError("series_ticker must not be blank")

    candidates = []
    seen_tickers = set()
    seen_cursors = set()
    cursor = None

    for _ in range(max_pages):
        params = {
            "status": "open",
            "mve_filter": "exclude",
            "limit": min(page_size, limit - len(candidates)),
        }
        if series_ticker is not None:
            params["series_ticker"] = series_ticker
        if cursor is not None:
            params["cursor"] = cursor
        data = fetch_json(client, "markets", params=params)
        markets = data.get("markets")
        if not isinstance(markets, list):
            raise ValueError("Expected a markets array")
        if "cursor" not in data:
            raise ValueError("Missing pagination cursor")
        next_cursor = data["cursor"]
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ValueError("Expected a string or null pagination cursor")

        for market in markets:
            if not isinstance(market, dict):
                raise ValueError("Expected each market to be an object")
            for field in ("ticker", "title", "status", "market_type"):
                if not isinstance(market.get(field), str) or not market[field].strip():
                    raise ValueError(f"Market has missing or invalid {field}")
            if market["status"] != "active" or market["market_type"] != "binary":
                continue
            ticker = market["ticker"]
            if ticker in seen_tickers:
                continue
            seen_tickers.add(ticker)
            candidates.append(market)
            if len(candidates) == limit:
                return candidates

        if not next_cursor:
            return candidates
        if next_cursor in seen_cursors:
            raise ValueError("Repeated pagination cursor; refusing to loop")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    logging.warning("Page cap reached; found %s of %s requested markets", len(candidates), limit)
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="Candidate count, 1–100 (default 10)")
    parser.add_argument("--series", help="Optional series ticker, e.g. KXHIGHNY")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Items per request, 1–100")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Request cap, 1–10")
    args = parser.parse_args()
    try:
        with httpx.Client(base_url=BASE_URL + "/", timeout=TIMEOUT) as client:
            markets = discover_markets(client, args.limit, args.series, args.page_size, args.max_pages)
        print(f"LIVE active binary-market candidates: {len(markets)} of {args.limit} requested")
        for market in markets:
            print(f"{market['ticker']} | {market['title']}")
    except (httpx.HTTPError, ValueError) as error:
        logging.error("Discovery failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

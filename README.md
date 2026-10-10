# Prediction Market Engine

Current scope: milestone 1, step 1A only. Fetch one Kalshi market and its
order book, then print the decoded JSON without changing the response fields.
Normalization, discovery, persistence, and serving an API are not implemented.

## Run

From this directory, using Python 3.12:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python fetch_raw.py KXHIGHNY-26OCT10-T73
```

The ticker above was active during the live check; it will eventually close.
Supply another exact Kalshi ticker when needed. For a manual way to find one:

```sh
curl --fail --silent --show-error --connect-timeout 5 --max-time 10 'https://external-api.kalshi.com/trade-api/v2/markets?series_ticker=KXHIGHNY&status=open&limit=1'
```

This is a manual selection aid, not an implemented discovery command. No API
key is required for these public endpoints.

```sh
.venv/bin/python fetch_raw.py --help
.venv/bin/python -m py_compile fetch_raw.py
.venv/bin/python -m pip check
```

## Code and dependency choices

`main()` reads the positional ticker, safely encodes it for a URL path, opens
one HTTPX client, and calls `fetch_json()` for each endpoint sequentially.
`fetch_json()` performs GET, prints the URL/status to stderr, rejects HTTP
errors, decodes JSON into a dictionary, and returns it. `json.dumps()` formats
that dictionary for stdout. JSON strings remain strings; no Decimal conversion
or bid/ask calculation happens yet. Printed JSON preserves fields and values,
but is reformatted rather than a byte-for-byte copy of the HTTP body.

HTTPX is the only direct dependency: outgoing synchronous HTTP, connection
reuse, explicit timeouts, and HTTP exceptions. Its installed dependencies are
pinned too: httpcore/h11 implement the transport and HTTP protocol, certifi
supplies trusted TLS certificates, idna handles international domain names,
and anyio/typing_extensions support HTTPX's internal concurrency and typing.
Our application uses synchronous functions. argparse, json, sys, and
urllib.parse are part of Python's standard library.

`httpx.Timeout(10.0, connect=5.0)` sets a five-second connection timeout and
ten-second read/write/pool timeouts. These bound inactivity in network phases,
not total wall-clock duration. Requests have no application retries in 1A.
Failures print an error and exit with status 1. If the order-book request fails,
the already printed market response remains visible.

Design choice: a single synchronous client and two sequential requests make
the flow easy to trace and reuse a connection. They take longer than parallel
requests and are not an atomic snapshot: the market can change between calls.

## Live evidence and trace

Live check: October 9, 2026 (America/Los_Angeles), Python 3.12.1,
HTTPX 0.28.1. Both requests returned HTTP 200 and the program exited 0.
Sandboxed execution initially failed DNS resolution; the live check succeeded
with approved network access. No fixture data was used.

Concrete trace from that run:

1. `args.ticker` was `KXHIGHNY-26OCT10-T73`; URL encoding left it unchanged.
2. `fetch_json(client, "markets/KXHIGHNY-26OCT10-T73")` requested
   `https://external-api.kalshi.com/trade-api/v2/markets/KXHIGHNY-26OCT10-T73`.
3. After HTTP 200, `response.json()` returned a dictionary containing `market`.
   `market["market"]["status"]` was `"active"` and the title was
   `"Will the maximum temperature be >73° on Oct 10, 2026?"`.
4. The second call used `markets/KXHIGHNY-26OCT10-T73/orderbook` and returned
   a dictionary containing `orderbook_fp`. Its `yes_dollars` was `[]`;
   its `no_dollars` included `["0.9900", "706.25"]`.
5. `json.dumps(orderbook, indent=2)` printed the response. Those two values
   stayed strings, and the empty list stayed empty. No price was invented.

Relevant syntax: `f"markets/{ticker}"` substitutes a variable into a string;
`with ... as client` closes the client even on error; `-> dict` is a type hint,
not runtime validation (the explicit `isinstance` check validates the root).
`raise_for_status()` raises on an unsuccessful HTTP status. `except` turns
HTTP and JSON/root-shape errors into a concise failure. The `__name__` guard
runs `main()` only when executed as a script; `sys.exit()` exposes its result
to the shell. `flush=True` prints output immediately.

Official documentation checked during implementation:

- [Public market data](https://docs.kalshi.com/getting_started/quick_start_market_data)
- [Get Market](https://docs.kalshi.com/api-reference/market/get-market)
- [Get Market Orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [Order-book fields and semantics](https://docs.kalshi.com/getting_started/orderbook_responses)

## Learning checkpoint

Your modification: change the two `indent=2` arguments to `indent=4`, then run
the same fetch command. Predict which parts of the output will change. Live
market values may change independently between runs.

1. Why call `raise_for_status()` before `response.json()`? What happens on 404?
2. In the observed book, what does `yes_dollars: []` tell us, and what do the
   two strings in `["0.9900", "706.25"]` represent?

Step 1A is complete; milestone 1 is still incomplete. Pause here before 1B.

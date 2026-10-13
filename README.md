# Prediction Market Engine

Milestone 1 is implemented and verified: raw fetching, exact quote
normalization, bounded discovery, and 85 offline tests. Atul cleared
the understanding checkpoints. Storage, an API, and scheduling remain
planned in the [original PRD](Prediction_Market_Engine_PRD.md).

## Run

From this directory, using Python 3.12:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python discover_markets.py --series KXHIGHNY --limit 3 --page-size 2
.venv/bin/python fetch_quote.py KXHIGHNY-26OCT12-B75.5
.venv/bin/python -m pytest -q
```

Use a ticker printed by discovery for the quote command; the ticker above is
from the historical live check and may close. `requirements-dev.txt` installs
the runtime packages plus tests/build tools. The editable (`-e`) installation
registers the modules and command names while keeping code edits immediately
visible. It uses the already installed build tools and dependencies.

Installed commands are also available:

```sh
.venv/bin/kalshi-discover --series KXHIGHNY --limit 3 --page-size 2
.venv/bin/kalshi-quote KXHIGHNY-26OCT12-B75.5
.venv/bin/kalshi-raw KXHIGHNY-26OCT12-B75.5
```

For runtime-only use of the scripts, install requirements.txt instead; the
editable installation and test tools are optional. To find a ticker manually:

```sh
curl --fail --silent --show-error --connect-timeout 5 --max-time 10 'https://external-api.kalshi.com/trade-api/v2/markets?series_ticker=KXHIGHNY&status=open&limit=1'
```

This curl command is a manual selection aid. No API key is required for these
public endpoints. The implemented discovery command is explained below.

```sh
.venv/bin/python fetch_raw.py --help
.venv/bin/python -m py_compile fetch_raw.py
.venv/bin/python -m pip check
```

## Step 1A: code and dependency choices

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

## Step 1A checkpoint (historical)

Your modification: change the two `indent=2` arguments to `indent=4`, then run
the same fetch command. Predict which parts of the output will change. Live
market values may change independently between runs.

1. Why call `raise_for_status()` before `response.json()`? What happens on 404?
2. In the observed book, what does `yes_dollars: []` tell us, and what do the
   two strings in `["0.9900", "706.25"]` represent?

Atul answered the revised questions in chat correctly. Implementation continued
to 1B on explicit instruction.

## Step 1B: normalize one quote

Goal: convert an order book into YES bid/ask, quantities, spread, and collection
time. `normalize_quote.py` holds validation, calculation, and the Pydantic
`Quote` model. `fetch_quote.py` reuses the original HTTP helper and timeouts.
`fetch_raw.py` still prints raw data unchanged.

Pydantic is the new direct dependency. It validates the normalized field types,
bounds, and precision, then serializes Decimal values as JSON strings. Its
installed dependencies are pinned in requirements.txt: pydantic_core executes
validation/serialization, annotated-types supplies constraint metadata, and
typing-inspection helps interpret type annotations. typing_extensions was
already installed. Decimal, datetime, pathlib, re, and logging are built in.

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python fetch_quote.py FIXTURE-EXAMPLE --fixture fixtures/two_sided.json
.venv/bin/python fetch_quote.py KXHIGHNY-26OCT12-T76
.venv/bin/python fetch_quote.py --help
.venv/bin/python -m py_compile fetch_raw.py fetch_quote.py normalize_quote.py
.venv/bin/python -m pip check
```

The live ticker is from the October 11 smoke check; select a current ticker with
the manual curl command above if it has closed. The fixture command makes no
HTTP calls. Its printed timestamp is the local inspection time, not an exchange
event time. The fixture marker and output label identify the data as synthetic.

Rules checked against [Kalshi fixed-point documentation](https://docs.kalshi.com/getting_started/fixed_point_migration)
on October 11, 2026: prices support up to four decimal places; quantities up to
two. The parser requires unsigned fixed-point strings, finite nonnegative
numbers, prices at most one, and supported precision. Scientific notation and
extra decimal places (including extra trailing zeros) are rejected without
rounding. Per-market price-grid checks are not implemented in this step;
observed prices are never snapped to another value.

Both side arrays must exist. Valid empty arrays yield None in Python and null
in JSON. A malformed level rejects the whole observation with an error reason;
the command exits 1 without printing a normalized quote. Valid zero-sized levels
are ignored after validation. All other levels are scanned for the highest
bid, independent of order. A crossed quote retains its negative spread and
prints a warning. No source book timestamp is supplied by the documented
endpoint, so source_timestamp remains null; metadata updated_time is not reused.

Concrete synthetic fixture trace:

1. `main()` reads fixtures/two_sided.json and labels it FIXTURE.
2. `normalize_orderbook()` requires the orderbook_fp object and both side arrays.
3. `best_bid()` calls `parse_decimal()` for every level. For YES, "0.4200"
   becomes Decimal("0.4200") and "10.00" becomes Decimal("10.00"). The lower
   "0.2000" level does not replace the best level.
4. Best YES is (Decimal("0.4200"), Decimal("10.00")); best NO is
   (Decimal("0.5500"), Decimal("8.00")).
5. YES ask = Decimal("1") - Decimal("0.5500") = Decimal("0.4500"). Its
   quantity is Decimal("8.00"). Spread = 0.4500 - 0.4200 = Decimal("0.0300").
6. The Quote model validates the result. model_dump_json() prints prices and
   quantities as strings and is_crossed as false.

`Decimal("0.42")` starts with exact decimal text. `Decimal(float_value)` would
preserve the float's rounding error, so the parser rejects numeric JSON values.
`Decimal | None` means a field may contain an exact number or missing data.
`yes_bid, yes_quantity = best_bid(...)` unpacks the returned two-value tuple.
The conditional expression `value if condition else None` calculates a value
only when its required input exists. Use `is not None` rather than truthiness:
a valid price of Decimal("0") must remain a price. `continue` skips a zero-size
level; it does not stop checking later levels. `Field(...)` defines Pydantic's
validation rules; strict mode prevents implicit numeric conversions.

Design tradeoff: rejecting a whole book on any malformed level sacrifices a
potentially usable partial quote, but avoids falsely describing an incomplete
or changed response as a complete observation. Empty liquidity is accepted;
broken data is reported separately.

Verification on October 11, 2026 (America/Los_Angeles):

- Fixture trace produced bid "0.4200", ask "0.4500", ask quantity "8.00",
  spread "0.0300".
- Manual assertions checked empty/one-sided books, unordered levels, subpenny
  prices, fractional quantities, zero quantities versus zero price, crossed
  quotes, timezone validation, JSON strings, and rejection of 19 invalid books.
  A malformed fixture command exited 1 and logged the missing-key reason.
- Syntax, CLI help, and installed dependency checks passed. The reusable pytest
  suite belongs to 1C and has not been added yet.
- Live fetch succeeded after approved network access (sandboxed DNS failed).
  Both endpoints returned HTTP 200 for active KXHIGHNY-26OCT12-T76. The observation
  time was 2026-10-12T04:15:07.575873Z (October 11 in Los Angeles). Output:

```json
{
  "ticker": "KXHIGHNY-26OCT12-T76",
  "observed_at": "2026-10-12T04:15:07.575873Z",
  "source_timestamp": null,
  "yes_bid": null,
  "yes_bid_quantity": null,
  "yes_ask": "0.0100",
  "yes_ask_quantity": "158.00",
  "spread": null,
  "is_crossed": false
}
```

## Step 1B learning checkpoint (historical)

Your modification: in fixtures/two_sided.json, change the NO price "0.5500"
to "0.6000". Predict the ask, spread, and crossed flag before running:

```sh
.venv/bin/python fetch_quote.py FIXTURE-EXAMPLE --fixture fixtures/two_sided.json
```

1. Why does the YES ask use the best NO bid's quantity rather than the best
   YES bid's quantity?
2. How should the program behave differently for `yes_dollars: []` versus a
   missing yes_dollars key, and why?

Atul answered the simplified follow-up correctly: a NO bid of 50 cents implies
a YES ask of 50 cents, which is not crossed against a 42-cent YES bid.

## Step 1C: bounded discovery and reusable tests

`discover_markets.py` lists candidates with tickers and titles. Inputs are
`--limit` (1–100, default 10), optional `--series`, `--page-size` (1–100,
default 25), and `--max-pages` (1–10, default 5). HTTP uses the existing explicit
timeouts. There are no application retries yet; HTTP errors/timeouts exit 1.

A page is one response containing a markets array and a cursor. The cursor is
an opaque continuation token: send it unchanged to ask for the next page.
Kalshi's list filter is status=open; the returned markets checked here use
status=active. The request also excludes multivariate combos. Each candidate
must have a ticker, title, status, and market_type; only active binary records
are kept. Duplicate tickers do not count twice.

The loop stops when it has enough candidates, the cursor is empty/null, or the
page cap is reached. A cap returns fewer candidates with a warning; it does
not claim an exhaustive list. Repeated cursors or malformed pages raise errors.
Each request asks for min(page size, remaining candidate count). The cap bounds
requests, not total elapsed time; phase timeouts remain inactivity limits.

Concrete live trace from October 11, 2026 (America/Los_Angeles):

```sh
.venv/bin/python discover_markets.py --series KXHIGHNY --limit 3 --page-size 2
```

1. `main()` passes limit=3, series_ticker="KXHIGHNY", page_size=2 to
   `discover_markets()`. The candidates list starts empty.
2. `min(2, 3 - 0)` is 2, so `fetch_json()` sends GET /markets with limit=2,
   status=open, mve_filter=exclude, and series_ticker=KXHIGHNY; no cursor yet.
3. HTTP 200 returns KXHIGHNY-26OCT12-T76 and KXHIGHNY-26OCT12-T69, both active
   binary markets, plus a cursor. The list now contains two candidates.
4. `min(2, 3 - 2)` is 1. The second request includes the returned cursor and
   limit=1. HTTP 200 returns KXHIGHNY-26OCT12-B75.5.
5. The list has three candidates, so the function returns immediately. `main()`
   prints their tickers/titles. No third page is requested.

`range(max_pages)` limits loop iterations; `len(candidates)` counts selected
markets; `min(...)` selects the smaller request size. `params["cursor"] = cursor`
adds a query parameter; HTTPX encodes query values safely. `.append(market)`
adds one item to a list. A set tracks previously seen tickers/cursors.

Tradeoff: bounded sequential discovery is easy to understand and avoids scanning
the exchange. It may return fewer candidates than requested when the cap is
reached, and pages can change between requests. It does not promise a stable or
exhaustive exchange snapshot.

### Tests and packaging

pytest is the new test dependency. A test's `assert` states an expected result:
if the calculation differs, the test fails. `pytest.raises(ValueError)` checks
that invalid data is rejected. Parametrization reruns the same behavior check
for several inputs. `httpx.MockTransport` supplies synthetic responses instead
of contacting Kalshi; an automatic fixture blocks unintended real HTTP calls.

`tests/fixtures/orderbooks.json` contains explicitly synthetic books. Tests use
that file and synthetic market pages rather than the editable practice fixture,
so your practice price changes do not alter their expected values.

- Normalization: corresponding ask quantity, unsorted books, missing sides,
  exact subpenny/fractional values, zero prices versus zero quantities, crossed
  values, malformed shapes/strings, nonfinite values, bounds, and precision.
- HTTP/discovery: cursor propagation, enough-results stopping, exhausted pages,
  partial results at the cap, repeated cursors, duplicate/nonactive filtering,
  input bounds, malformed pages, HTTP errors, and explicit timeout failures.
- Commands: fixture labeling/no HTTP, invalid fixture exit status, and discovery
  timeout exit status without fabricated candidates.

`requirements-dev.txt` pins pytest and its installed dependencies: iniconfig
reads configuration, packaging handles versions, pluggy supports pytest's
extension mechanism, and pygments formats reported source/errors. setuptools
builds/installs our modules and the three commands declared in pyproject.toml.
No runtime dependency was added in 1C. Package version 0.0.1 is development
metadata, not a completed v0.1 backend release.

Verification on October 11, 2026 (America/Los_Angeles): **85 offline tests passed**.
Editable installation, command help, and dependency checks passed. Live discovery
returned three candidates in two HTTP 200 requests after approved network access
(sandboxed DNS failed). The selected quote command also returned HTTP 200 from
both endpoints for an active binary market:

```sh
.venv/bin/python fetch_quote.py KXHIGHNY-26OCT12-B75.5
```

```json
{
  "ticker": "KXHIGHNY-26OCT12-B75.5",
  "observed_at": "2026-10-12T05:38:42.621749Z",
  "source_timestamp": null,
  "yes_bid": "0.0200",
  "yes_bid_quantity": "169.00",
  "yes_ask": "0.0300",
  "yes_ask_quantity": "44.00",
  "spread": "0.0100",
  "is_crossed": false
}
```

This is live output, not a fixture. Its UTC date is October 12; its Los Angeles
date is October 11. Milestone 1's technical acceptance checks are satisfied.
Suggested commit after review: `feat: ingest and normalize Kalshi market quotes`.
No database, scheduled worker, retries, or query API is included.

References checked for 1C:

- [Kalshi Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)
- [Kalshi pagination](https://docs.kalshi.com/getting_started/pagination)
- [pytest parametrization](https://docs.pytest.org/en/stable/how-to/parametrize.html)
- [setuptools configuration](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)

## Step 1C learning checkpoint

Your modification: change DEFAULT_PAGE_SIZE in discover_markets.py from 25
to 1, then run without a --page-size override:

```sh
.venv/bin/python discover_markets.py --series KXHIGHNY --limit 3
.venv/bin/python -m pytest -q
```

Expect three GET requests rather than one when three eligible markets are
available. The output still contains at most three candidates; live data can
change. The tests specify page sizes where their exact request counts matter.

1. What does the cursor let the second request do that repeating the first
   request would not?
2. If limit=3, page_size=1, and max_pages=2, why can discovery return only two
   candidates even when more markets exist?

Atul cleared the simplified 1C checkpoint. Milestone 1 is complete; step 2A follows.

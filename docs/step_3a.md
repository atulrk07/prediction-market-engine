# Step 3A: ask our backend for stored market details

Verified October 13, 2026 (America/Los_Angeles). This step adds four read-only
routes. No new migration or database reset was needed. Existing data and Atul's
replay_quote.py display modification were preserved. The learning checkpoint
below is pending; do not implement 3B yet.

## Goal, input, output, and files

Instead of invoking a script for every read, run a server and send it URLs.
A GET request asks for information; its response includes a status code and JSON.
Incoming HTTP goes from your browser/curl to our API. Earlier outgoing HTTP
went from our Python collector to Kalshi. These routes query PostgreSQL only.

| Request | Input | Output |
| --- | --- | --- |
| GET /health | None | 200, {"status": "ok"}; no database connection |
| GET /ready | None | 200, {"status": "ready"}; 503 if the market table cannot be read |
| GET /markets | limit (default 20, 1–100), offset (default 0, nonnegative) | 200, an array of stored markets ordered by id; an empty page is [] |
| GET /markets/{id} | Positive database market ID | 200, stored metadata; 404 if missing |

IDs and offsets fit PostgreSQL's signed 64-bit range; invalid inputs return 422.
Closed markets and markets without snapshots still have queryable metadata.
Market status is whatever we last stored; this GET does not verify current
exchange status. updated_at records a metadata write, not a quote observation.

- api.py: typed response shapes, app lifecycle, shared connection dependency,
  and four synchronous route functions. /docs is the generated interactive UI;
  /openapi.json describes the routes, inputs, responses, and limits.
- market_queries.py: list_markets and find_market select stored metadata.
- tests/test_api.py: HTTP contracts and isolated PostgreSQL route checks.
- requirements.txt and pyproject.toml: pinned dependencies and packaged modules.
- README.md, PLAN.md, AGENTS.md: startup instructions and current progress.

## Dependencies

FastAPI 0.143.0 maps URLs to functions, validates inputs with our existing
Pydantic, and documents response schemas. Uvicorn 0.54.0 listens for HTTP
requests and runs the FastAPI app. No optional server extras were installed.

Supporting dependencies installed with them are pinned too: Starlette 1.8.0
provides HTTP/routing and the test client; Click 8.5.0 provides Uvicorn's command
line; annotated-doc 0.0.5 supplies documentation annotations; opentelemetry-api
1.45.1 supplies FastAPI's tracing interfaces. No exporter or tracing service
is configured here. Existing HTTPX is reused by the test client; it currently
emits one deprecation warning recommending httpx2. All tests still pass.

## Run it

From the repository root, with PostgreSQL running and migration 0001 applied:

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port 8000 --timeout-keep-alive 5 --timeout-graceful-shutdown 10
```

api:app means use the app object from api.py. The server stays running until
Ctrl+C. Open http://127.0.0.1:8000/docs, or run in a second terminal:

```sh
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 http://127.0.0.1:8000/health
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 http://127.0.0.1:8000/ready
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 'http://127.0.0.1:8000/markets?limit=1&offset=0'
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 http://127.0.0.1:8000/markets/1
curl --silent --show-error --include --connect-timeout 2 --max-time 10 http://127.0.0.1:8000/markets/9223372036854775807
curl --silent --show-error --include --connect-timeout 2 --max-time 10 'http://127.0.0.1:8000/markets?limit=101'
```

The last two commands intentionally allow HTTP errors so you can see 404 and
422 bodies. Use a market ID from your list when 1 is unavailable. No fixture
data is loaded into the application database by this step.

Database access reuses database.py: connect timeout 5 seconds, statement timeout
10 seconds, lock timeout 5 seconds, and pool wait timeout 5 seconds. Uvicorn
keep-alive timeout 5 seconds bounds idle connections; graceful shutdown timeout
10 seconds bounds waiting for active requests at shutdown. Neither is a total
request deadline. curl bounds each manual request to 10 seconds.

## Concrete stored-data trace

Actual GET /markets/1 returned 200 with previously live-collected metadata:

```json
{
  "id": 1,
  "platform": "kalshi",
  "external_id": "KXHIGHNY-26OCT14-B84.5",
  "title": "Will the maximum temperature be 84-85° on Oct 14, 2026?",
  "status": "active",
  "close_time": "2026-10-15T05:00:00Z",
  "updated_at": "2026-10-14T02:23:50.660071Z"
}
```

1. Uvicorn receives GET /markets/1 and passes it to app.
2. FastAPI matches /markets/{id}, parses the URL's "1" into integer 1, and
   checks its positive 64-bit bounds.
3. Depends(get_connection) obtains a connection from the shared engine. The
   engine was created at startup; database connections are opened when needed.
4. market_detail calls find_market(connection, 1). Its SQL is effectively
   SELECT ... FROM markets WHERE markets.id = :id, with bound value 1.
5. mappings().one_or_none() obtains one row with column-name keys, or None.
   The found row becomes a dictionary. If absent, the route raises HTTP 404.
6. MarketResponse checks the response fields and serializes aware datetimes
   to JSON strings. FastAPI returns it with HTTP 200. The connection is released.

There is no order-book request, new snapshot, or exchange refresh along this path.

## Syntax and tradeoff

@app.get("/markets/{id}") registers the function below it as the GET handler.
Annotated[int, Path(gt=0, ...)] says the URL value must be an integer greater
than zero, within the specified bound. Query(ge=1, le=100) sets list-limit
bounds. ge means greater than or equal; le means less than or equal.

Depends(get_connection) asks FastAPI to supply the connection argument.
yield connection lends it to the route; with closes/releases it afterward,
including on failure. The lifecycle's yield separates startup from shutdown.
Although lifecycle management uses async def, the route/query functions use
normal def and synchronous SQLAlchemy access.

markets.c.id selects the table's id column. order_by fixes the order, limit
caps returned rows, and offset skips rows. For IDs [1, 2, 3], limit=1&offset=1
returns ID 2. This is offset pagination, separate from Kalshi's discovery cursor.

Tradeoff: offset pagination is easy to understand and sufficient for a small
market list. Large offsets require skipping more rows, and concurrent changes
can shift pages. A fixed order is deterministic for unchanged data; it does
not freeze the list across separate requests.

## Verification

```sh
TEST_DATABASE_URL='postgresql+psycopg://prediction_market:local_dev_password@127.0.0.1:5433/prediction_market_test' .venv/bin/python -m pytest -q
.venv/bin/python -m pip check
```

129 tests passed on real PostgreSQL; pip found no broken requirements. Without
TEST_DATABASE_URL, 103 tests pass and 26 database tests are explicitly skipped.
API integration tests use synthetic UUID-tagged metadata in the isolated test
database and delete only their own rows. During HTTP tests, a SQL guard allows
SELECT statements only; the existing HTTP guard rejects outgoing real requests.

Actual localhost HTTP checks passed for health, readiness, ordered market list,
detail, an empty page, missing ID (404), invalid limit (422), and /docs (200).
The application response above is stored real exchange data, not a fixture.
An additional real HTTP check used a temporary API on port 8001 with its
DATABASE_URL pointed at unused localhost port 1: /health returned 200 and
/ready returned 503. The real PostgreSQL server was left running. Both smoke
test API processes were stopped cleanly afterward, freeing ports 8000/8001.
No new live Kalshi request was attempted in 3A. API quote/history routes and
automatic collection remain unimplemented; this is not full milestone 3.

## Your modification and checkpoint

In api.py, change the FastAPI title from "Prediction Market Data API" to
"Atul's Market API". Stop the server with Ctrl+C, restart with the same Uvicorn
command, and check the title at http://127.0.0.1:8000/docs. Or use:

```sh
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 http://127.0.0.1:8000/openapi.json | .venv/bin/python -c 'import json, sys; print(json.load(sys.stdin)["info"]["title"])'
```

1. If PostgreSQL is unavailable but the API is still running, which route
   returns 503: /health or /ready?
2. When you request /markets/1, does the API ask Kalshi or read PostgreSQL?

Wait for Atul's answers or explicit continuation before 3B.

Official references checked for this step:

- [FastAPI dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/)
- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)
- [FastAPI numeric validation](https://fastapi.tiangolo.com/tutorial/path-params-numeric-validations/)
- [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/)
- [Uvicorn server settings](https://www.uvicorn.org/settings/)

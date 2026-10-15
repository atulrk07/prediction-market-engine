# Prediction Market Engine

Collect Kalshi order-book quotes, store their history in PostgreSQL, and query
saved observations through a FastAPI API.

A separate worker polls a ticker watchlist every 60 seconds. The API serves
market metadata, latest quotes, and historical observations from the database,
including collection age and stale-data indicators.

```text
Kalshi → Worker → PostgreSQL → FastAPI → Client
```

Built with Python 3.12, HTTPX, Pydantic, SQLAlchemy, Alembic, and Docker Compose.

## Quick start

Requires Docker with Compose.

```sh
git clone https://github.com/atulrk07/prediction-market-engine.git
cd prediction-market-engine
cp watchlist.example.json watchlist.json
docker compose build
docker compose run --rm --no-deps worker kalshi-discover --series KXHIGHNY --limit 5 --page-size 5
```

Edit `watchlist.json` with tickers returned by discovery. It accepts a JSON
array of 1–10 unique binary-market tickers. The example file contains real,
dated tickers that can close; replace them with current active markets.

```sh
docker compose up -d --wait --wait-timeout 90 api worker
docker compose logs --tail 50 worker
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 http://127.0.0.1:8000/ready
```

Open the [dashboard](http://127.0.0.1:8000/dashboard) to see every tracked
market, its latest price and freshness, and a bid/ask chart with CSV download.
Open [API docs](http://127.0.0.1:8000/docs) to explore the endpoints.
Compose waits for PostgreSQL, applies migrations, then starts the API and one
worker. The migration container exits after applying the schema.

Stop the stack with `docker compose down`. The named `postgres_data` volume
retains the database. Local credentials in `compose.yaml` are for development;
services bind to localhost.

## API

| Endpoint | Description |
| --- | --- |
| `GET /dashboard` | Browser dashboard built on the routes below; `/` redirects here |
| `GET /health` | Process liveness |
| `GET /ready` | Database readiness; 503 when unavailable |
| `GET /markets?limit=20&offset=0` | Stored markets; maximum 100 per page |
| `GET /markets/{id}` | Stored market metadata |
| `GET /markets/{id}/quote` | Latest quote, spread, age, and stale flag |
| `GET /markets/{id}/history` | Historical observations; default last 24 hours |

```sh
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 'http://127.0.0.1:8000/markets?limit=5'
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 http://127.0.0.1:8000/markets/1/quote
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 'http://127.0.0.1:8000/markets/1/history?limit=100&offset=0'
```

Use an ID returned by `/markets` in place of `1`. Prices and quantities are
JSON strings to preserve exact decimals; missing sides are null. YES ask is
`1 - best NO bid`. For example, a synthetic YES bid of `"0.4200"` and NO bid
of `"0.5500"` produce a YES ask of `"0.4500"` and spread of `"0.0300"`.

Unknown markets return 404. Known markets without observations return 200 with
`status: "no_observations"` and null quote, age, and stale flag. Freshness and
market status are independent of whether the book has one or both quote sides.

History accepts timezone-aware `start` and `end`, includes start and excludes
end, and sorts by `(observed_at, id)`. Maximum range: seven days. Maximum page:
1,000 observations. Invalid ranges and pagination parameters return 422.

## Price alerts

Alert rules watch a market's saved YES ask. These routes have no authentication
and are intended for local/private use; keep the API bound to localhost or on
a trusted private network.

Use a database market ID from `GET /markets`. Thresholds are exact decimal
strings, strictly between 0 and 1 (`"0.40"` means $0.40 per contract):

```sh
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 \
  -X POST http://127.0.0.1:8000/alert-rules \
  -H 'Content-Type: application/json' -d '{"market_id":1,"threshold":"0.40"}'
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 \
  'http://127.0.0.1:8000/alert-rules?limit=20&offset=0'
curl --fail --silent --show-error --connect-timeout 2 --max-time 10 \
  -X POST http://127.0.0.1:8000/alert-rules/1/disable
```

Create returns 201; list and disable return 200. Replace the example rule ID in
the disable URL with the returned ID. Lists include disabled rules, ordered by
ID, with at most 100 per page. Unknown markets/rules return 404; invalid
thresholds or pagination return 422; database failures return 503.

Add your Discord webhook to the local `.env` file, which Git ignores. Never
commit this file or the webhook URL:

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
```

Compose passes this setting only to the worker. Apply migrations and start the
updated API and the existing single worker:

```sh
docker compose build
docker compose run --rm migrate
docker compose up -d --wait --wait-timeout 90 api worker
```

For a worker run directly with Python, set `DISCORD_WEBHOOK_URL` in that
process's environment and use the existing `worker.py --watchlist watchlist.json`
command. Do not run a second worker alongside the Compose worker.

After each cycle finishes collecting, the worker evaluates its newly committed
snapshots. An enabled rule sends when `YES ask < threshold`
and it has not already alerted. Its first observation can trigger it. A confirmed
delivery sets `is_alerted` and `last_alerted_at`; staying below does not send again.
An ask at or above the threshold resets `is_alerted`, allowing a later drop to
trigger another message. Missing asks and disabled rules are skipped. Messages
include market name/ticker, threshold, ask, and observation time.

Delivery is best effort, with a five-second HTTP timeout and no immediate retry.
An unset webhook or failed delivery leaves the rule eligible for a later snapshot.
A crash after sending but before saving rule state can cause a duplicate; an
alert-state database failure can do the same. Failed delivery is retried only
on a later newly saved snapshot, not by replay or a duplicate collection attempt.
Alerts run outside ingestion transactions, so failures cannot roll back snapshots
or turn a successful ingestion into a failure. Delivery is synchronous and can
lengthen collection cycles. The existing single-worker assumption still applies.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `API_PORT` | `8000` | Compose API port on the host |
| `POSTGRES_PORT` | `5433` | PostgreSQL port on the host |
| `STALE_AFTER_SECONDS` | `120` | Age after which a saved quote is stale |
| `DATABASE_URL` | Local PostgreSQL URL in `database.py` | Database connection for Python commands |
| `DISCORD_WEBHOOK_URL` | Unset | Optional worker-only Discord price-alert destination |

Set variables before starting Compose. For example, if the default ports are
occupied:

```sh
export API_PORT=8002
export POSTGRES_PORT=55433
```

Use the matching API port in browser and curl URLs. Containers connect to
`db:5432` internally. Run one worker instance.

## Collection and replay

For a short live collection, stop the continuous worker first:

```sh
docker compose stop worker
docker compose run --rm worker python worker.py --watchlist /app/watchlist.json --interval 10 --cycles 3
```

Inspect saved records using IDs from worker output or API responses:

```sh
docker compose run --rm --no-deps worker kalshi-run 1
docker compose run --rm --no-deps worker kalshi-read 1
docker compose run --rm --no-deps worker kalshi-replay 1
```

`kalshi-run` takes a run ID; `kalshi-read` and `kalshi-replay` take snapshot IDs.
Replace the example IDs with existing records. Replay uses saved values and the
original collection slot, keeping the first observation. A later slot creates
new history even when prices are unchanged. Resume continuous collection with
`docker compose up -d worker`.

## Local development and tests

Requires Python 3.12 and PostgreSQL. Start the Compose database, install the
application, and apply migrations:

```sh
docker compose up -d --wait --wait-timeout 90 db
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
export DATABASE_URL="postgresql+psycopg://prediction_market:local_dev_password@127.0.0.1:${POSTGRES_PORT:-5433}/prediction_market"
.venv/bin/python -m alembic upgrade head
```

To run Python processes directly, keep the Compose API/worker stopped and use
separate terminals with the same database settings:

```sh
.venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port 8000 --timeout-keep-alive 5 --timeout-graceful-shutdown 10
.venv/bin/python worker.py --watchlist watchlist.json --cycles 1
```

Create the isolated test database once, then migrate and run the full suite.
Omit `createdb` if it already exists:

```sh
docker compose exec -T db createdb -U prediction_market prediction_market_test
export TEST_DATABASE_URL="postgresql+psycopg://prediction_market:local_dev_password@127.0.0.1:${POSTGRES_PORT:-5433}/prediction_market_test"
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/python -m alembic upgrade head
.venv/bin/python -m pytest -q
```

Dashboard regression tests use Node.js 22 or later and its built-in test runner:

```sh
node --test tests/test_dashboard.cjs
```

Without `TEST_DATABASE_URL`, PostgreSQL tests skip. Exchange and Discord HTTP are mocked in
the test suite. GitHub Actions runs the tests with PostgreSQL, builds the images,
and checks API startup and synthetic data persistence across container recreation.

Local verification passed 324 Python tests and eight dashboard regression tests.
Chrome checks cover charts, CSV downloads, failed requests, and responses arriving
out of order. Docker checks verify startup, dashboard delivery, and saved data
surviving container recreation. A separate live smoke check collected 15 snapshots
across five markets over three cycles, with all 30 exchange requests succeeding.

## Design notes

The database enforces one observation per market and collection slot. Writes
commit per market, retries are bounded, and downtime leaves visible gaps.
Unfinished ingestion runs are reported as incomplete. Market routes read saved
data, so collection failures do not prevent access to existing history.

See [architecture](docs/architecture.md) for normalization, transaction, retry,
and query details.

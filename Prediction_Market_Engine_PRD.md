# Prediction Market Engine — v0.1 PRD and Build Plan

Status: implementation specification; features below are planned until built and verified.

Repository: `prediction-market-engine`

First-release title: Prediction Market Data Pipeline & API

## 1. Product decision

Build a Python backend that collects a small watchlist of Kalshi markets, normalizes order-book quotes, stores historical observations in PostgreSQL, and serves them through a FastAPI API. Make failures, retries, missing data, and duplicate prevention explicit.

The first release should demonstrate a complete path from an external API to durable storage to your own query API. Paper trading and Polymarket belong to later releases. The immediate deadline takes priority over the larger platform previously discussed.

Primary user: Atul, following prediction markets and building an explainable backend project alongside a Premier League ML predictor.

Practical use today: collect your own price history for markets you follow; query current observed bid/ask, spread, and collection age; inspect how prices changed while you were away. This version supplies data for research. It does not estimate fair probabilities or identify profitable trades.

## 2. Scope and completion gates

Required for the first complete release:

- Select 5–10 active binary markets through a configured ticker watchlist.
- Fetch market metadata and order books from Kalshi.
- Normalize YES bid/ask prices and associated quantities.
- Persist observations in PostgreSQL using migrations and database constraints.
- Query markets, latest quotes, and bounded historical data through FastAPI.
- Run a separate worker every 60 seconds by default.
- Handle timeouts, temporary upstream errors, and rate limits with bounded retries.
- Prevent duplicate observations when a collection is retried.
- Report collection age and partial failures honestly.
- Provide meaningful tests, Docker Compose setup, and a concise README.

Minimum functional checkpoint: milestones 1–3 work together against real Kalshi data and PostgreSQL. At this point the project is a manually refreshed data pipeline and API. Milestone 4 completes scheduled collection and release packaging.

Do not add to v0.1: Streamlit/React dashboard, paper trading, prediction models, alerts, automatic market matching, Polymarket, WebSockets, Redis, Celery, Kafka, Kubernetes, user accounts, cloud hosting, or automated real-money orders. FastAPI's interactive API documentation is the first demo interface.

## 3. Stack and boundaries

| Tool | Responsibility |
| --- | --- |
| Python | Application language |
| HTTPX | Outgoing exchange requests |
| Pydantic | Input and normalized-data validation |
| FastAPI | Incoming read-only API requests |
| PostgreSQL | Durable data, constraints, and queries |
| SQLAlchemy | Database models and access |
| Alembic | Versioned schema migrations |
| pytest | Normalization, failure, API, and database tests |
| Docker Compose | Reproducible database, API, and worker processes |
| GitHub Actions | Offline unit tests and PostgreSQL integration tests |

Use synchronous application code initially. Start with one sequential worker; asynchronous concurrency is a later optimization if collection time warrants it. Pin compatible dependencies after installation succeeds.

Separate exchange parsing, ingestion logic, database access, and API routes. The API reads stored data; it never refreshes Kalshi implicitly during a GET request. API and worker reuse the same application package. Keep the implementation small; introduce abstractions only when they remove actual duplication.

## 4. Functional requirements

### A. Market discovery and quote normalization

Provide a small discovery command that lists active candidates and their tickers. Fetch enough pages to satisfy a bounded requested limit; handle pagination without requesting the entire exchange. Users select tickers for a watchlist configuration.

Consult current official Kalshi documentation before implementing the adapter. The documented order-book format uses `orderbook_fp.yes_dollars` and `orderbook_fp.no_dollars`, containing price/quantity strings.

- Best YES bid: maximum available YES bid price.
- Best YES ask: `Decimal('1') - maximum NO bid price`.
- YES ask quantity: quantity at that corresponding NO bid.
- Do not assume an array is already sorted.
- Empty side: price and quantity are `null`, not zero.
- Spread: ask minus bid only when both are available.
- Parse monetary values directly from strings into `Decimal`; never through a float.
- Validate finite prices between 0 and 1 and valid nonnegative quantities. Ignore zero-sized levels. Reject malformed records with a logged reason.
- Preserve each observation's UTC collection timestamp. Store an exchange timestamp separately only when the source supplies one.
- A quote price is a market observation, not a calibrated probability forecast.

Fixtures should cover two-sided, one-sided, empty, unordered, and malformed books. Keep any example market values explicitly labeled as fixtures.

### B. Persistence and duplicate handling

Use an application-generated UTC collection slot, floored to the configured polling interval, as the identity of a scheduled collection. Keep this slot unchanged across retries. All markets within a cycle share the same slot, but each has its own actual observation time.

Database uniqueness: `(market_id, collection_slot)`.

- The first valid observation for a market/slot wins; repeated inserts use conflict handling and leave that observation intact.
- The same price in a later slot is a new, valid historical observation.
- Never deduplicate solely by price.
- Do not fill downtime with invented historical observations on restart. Resume current collection and leave gaps visible.
- Commit each market's metadata upsert and snapshot insert in one transaction. A failed market must not roll back successful markets from that cycle.
- Failed HTTP requests do not create quote rows. A successfully fetched empty book may create a snapshot with nullable quotes.

### C. Query API

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Process liveness; no upstream exchange call |
| `GET /ready` | Database readiness; 503 when unavailable |
| `GET /markets?limit=20&offset=0` | Tracked markets in a stable order; validate and cap limit at 100 |
| `GET /markets/{id}` | Stored metadata; 404 for unknown market |
| `GET /markets/{id}/quote` | Latest snapshot, bid, ask, spread, observed time, age, and stale flag |
| `GET /markets/{id}/history?start=...&end=...&limit=100&offset=0` | Time-bounded snapshots in stable chronological order |

History: default to the most recent 24 hours when no range is provided; cap a request's range at 7 days and rows at 1,000. Reject reversed ranges, negative offsets, invalid timestamps, and invalid limits. Use a half-open range `[start, end)` with UTC timezone-aware timestamps. Order by `(observed_at, id)` for deterministic results. Offset pagination is an intentional small-scale choice; cursor pagination can follow later.

Known market without observations: return an explicit `no_observations` response, never a fabricated price. Unknown market: 404. Expose decimal values as JSON strings consistently and document this in the response schema.

`age_seconds` measures time since collection; `is_stale` defaults to true after 120 seconds and is configurable. This indicates collection freshness, not proof that an upstream resting order was recently updated. An empty book and a stale quote are different states.

### D. Worker and failures

- Separate worker process with a configurable interval; default 60 seconds.
- One worker instance for v0.1. Run cycles sequentially so they cannot overlap within that process.
- Bound request duration; initial request timeout target: 10 seconds.
- Maximum three HTTP attempts per request for connection failures, timeouts, HTTP 429, and selected temporary 5xx failures.
- Use exponential backoff with jitter. Respect a valid `Retry-After`; if the delay exceeds the allowed cycle retry budget, skip this cycle rather than retrying early.
- Do not retry permanent 4xx errors or invalid response schemas indiscriminately.
- Continue with other markets when one fails; record a partial run.
- If a cycle overruns, skip elapsed slots and proceed to the next current cycle. Do not create a catch-up request storm.
- Graceful shutdown and clean connection closure.

Run logs should include run ID, ticker, duration, attempt count, inserted/duplicate/failed counts, and concise errors. Do not log credentials. A run left incomplete by a crash is not a success; expose it honestly in logs/database records rather than claiming automatic recovery of its status.

## 5. Database schema

| Table | Minimum fields and constraints |
| --- | --- |
| `markets` | `id`, `platform`, `external_id`, `title`, `status`, `close_time`, `updated_at`; unique `(platform, external_id)` |
| `snapshots` | `id`, `market_id` FK, `collection_slot`, `observed_at`, nullable `source_timestamp`, nullable YES bid/ask and quantities; unique `(market_id, collection_slot)` |
| `ingestion_runs` | `id`, `collection_slot`, `started_at`, `finished_at`, status, attempted/inserted/duplicate/failed counts, error summary |

Use timezone-aware PostgreSQL timestamps and exact numeric columns for prices and quantities. Select numeric precision that preserves documented exchange precision; reject unsupported precision instead of silently rounding it. Add numeric bounds/check constraints where applicable.

Add an index on `(market_id, observed_at, id)` for latest/history access. Explain the read benefit and additional write/storage cost in the README. No claim of a performance improvement without a before/after measurement.

`platform` is `kalshi` in this release; it is a lightweight provision for a future adapter, not evidence that multiple exchanges are supported.

## 6. Four commit-sized milestones

### Milestone 1 — Fetch and normalize

Deliver: minimal package/configuration, bounded market discovery, one-shot quote command, normalized quote schema, timeout handling, and normalization tests.

Done: fetch and print one real active market quote; fixtures verify ask derivation, missing sides, decimals, and invalid data.

Understand: outgoing HTTP requests, JSON, validation, bid/ask mapping, Decimal.

Suggested commit: `feat: ingest and normalize Kalshi market quotes`

### Milestone 2 — Store durable history

Deliver: PostgreSQL Compose service, models, initial Alembic migration, persistence, collection-slot identity, and integration tests.

Done: ingest the same slot twice without duplicate snapshots; ingest a new slot even if prices are unchanged; restart the app and query previously saved data. Tests run against PostgreSQL, not a substituted SQLite database.

Understand: primary/foreign keys, unique constraints, transactions, upserts, migrations.

Suggested commit: `feat: persist idempotent market snapshots in PostgreSQL`

### Milestone 3 — Serve the data

Deliver: documented FastAPI routes, typed responses, query limits, correct empty/error responses, freshness calculation, and route/database tests.

Done: run a real one-shot ingestion, then use `/docs` or curl to retrieve latest and historical quotes. A GET request does not call Kalshi. The API continues serving saved data during an upstream outage and marks old observations stale.

Understand: incoming versus outgoing HTTP, dependency injection, database queries, API contracts, index purpose.

Suggested commit: `feat: expose market history and freshness through FastAPI`

This is the minimum functional checkpoint for describing a working pipeline and API on a resume. State manual refresh if scheduling is not yet implemented.

### Milestone 4 — Automate and make the demo reproducible

Deliver: separate scheduled worker, bounded retry policy, partial-failure run tracking, API/worker Compose services, offline CI, documented startup/migration/test commands, and a short demo transcript or screenshot.

Done: collect several real cycles; demonstrate timeout recovery, duplicate prevention, and stale-data reporting; run tests without live exchange access. Start from a clean checkout using the README. Ensure database migrations run before services rely on the schema.

Understand: retry classification, idempotency versus scheduling, fault isolation, configuration, logs, reproducible environments.

Suggested commit: `feat: automate reliable ingestion and document reproducible demo`

After verification, an optional `v0.1.0` tag identifies the finished first release. Commit each working milestone rather than waiting for the whole release. Do not pause implementation solely because a commit cannot be pushed.

## 7. Meaningful verification

Required behaviors across the milestones:

1. YES ask and its quantity come from the corresponding best NO bid.
2. Missing book sides remain null and subpenny values remain exact.
3. Replaying a market/slot produces one snapshot, including competing inserts enforced by the database constraint.
4. An unchanged price in a new slot produces a new snapshot.
5. A failing market does not prevent another market's snapshot from committing.
6. Temporary HTTP errors retry within limits; permanent errors do not retry indefinitely. Mock time/sleep in tests.
7. Invalid history inputs fail clearly; valid history is bounded and ordered.
8. Freshness changes under a controlled clock; an unknown market differs from one with no observations.
9. API reads work without network calls to Kalshi.

Integration tests use an isolated test database. Normal CI uses fixtures/mocked HTTP so a network outage or changing market cannot break it. Run one separate manual live smoke check and record its date. Do not claim live integration is verified if access is blocked.

## 8. Demo and documentation

README should contain: product purpose, shipped versus planned scope, architecture/data flow, exact setup and migration commands, sample API responses, tests, reliability decisions, and known limitations.

Demonstrate this sequence:

1. Start PostgreSQL and apply migrations.
2. Discover and configure a few real active markets.
3. Collect quotes and inspect latest/history responses.
4. Replay a collection slot and show no duplicate rows.
5. Run the worker for multiple cycles.
6. Stop the worker, wait past the configured freshness threshold, and show the stale flag.

A clearly labeled fixture/demo mode is optional if useful for offline review. It must not be presented as live data. Docker Compose should preserve database data in a named volume. Bind the first local demo to localhost; public hosting is a separate future task.

## 9. Resume wording matched to implementation

Project title: **Prediction Market Engine | Python, FastAPI, PostgreSQL, Docker**. List Docker only once you have actually used the Compose setup.

After milestones 1–3 are verified, these two bullets are appropriate:

- Built a Python ingestion pipeline and FastAPI service to normalize Kalshi order-book data and query historical market snapshots in PostgreSQL.
- Implemented transactional writes, database-enforced deduplication, and indexed history queries with integration tests for data consistency.

After milestone 4 is verified, a third bullet can be added:

- Automated market-data collection with bounded retries, per-market failure isolation, and stale-data reporting; packaged the service with Docker Compose and automated tests in GitHub Actions.

Remove or narrow any clause whose feature is not finished. Do not claim Polymarket support, paper trading, production deployment, profitability, throughput, uptime, or percentage improvements from the roadmap alone. Add measured quantities later with their context.

## 10. Learning workflow and interview checkpoints

Understanding the code is a delivery requirement. Split each milestone into the following small, runnable steps and complete only one step before a learning checkpoint:

| Milestone | Small steps |
| --- | --- |
| 1 | 1A: fetch and inspect one raw market/order-book response; 1B: normalize it with Decimal and explicit missing-data handling; 1C: add bounded discovery and normalization tests |
| 2 | 2A: define tables and apply the migration; 2B: save and query one observation; 2C: add duplicate prevention and prove it with PostgreSQL tests |
| 3 | 3A: health/readiness and market routes; 3B: latest quote and freshness; 3C: bounded history and route tests |
| 4 | 4A: separate polling worker; 4B: retries and per-market failure handling; 4C: reproducible Compose startup, CI, and README |

At every small step:

1. Before coding, explain the immediate problem, inputs, outputs, files, and any essential new concept in plain English.
2. Implement just that step using straightforward functions and minimal abstractions. Explain why each new dependency is needed.
3. Run the relevant code/tests and trace one concrete input through the important functions, showing the intermediate values.
4. Explain unfamiliar syntax where it affects behavior and one design choice with its tradeoff.
5. Give Atul one small modification to make himself, with the command needed to check it.
6. Ask two specific understanding questions and wait for his answer or explicit instruction to continue. Do not build the rest of the milestone ahead of the checkpoint.

Keep explanations tied to the code just written. Do not require memorizing framework internals, and do not turn each checkpoint into a broad lecture. If understanding is incomplete, use a smaller worked example before continuing. Commit at completed milestone boundaries after the relevant behaviors are verified.

Example checkpoint using fictional data:

```python
yes_bids = [["0.42", "10"]]
no_bids = [["0.55", "8"]]
```

Expected normalized values: YES bid 0.42 with quantity 10; YES ask 0.45 with quantity 8; spread 0.03. Ask Atul to change a price and predict the result, then make one side empty and explain which values become null.

The core understanding goal remains the full path of one quote: external HTTP response, normalized Python object, durable database row, and your own API response.

Interview checkpoints:

Be able to trace one quote from HTTP response to database row to API response. Explain:

- Why PostgreSQL uniqueness is stronger than only checking for a row in Python.
- Why a retry and a later unchanged-price observation are different.
- What a transaction commits and what happens on failure.
- Why bid, ask, midpoint, and last traded price are not interchangeable.
- Why the API reads the database instead of fetching upstream on every request.
- Why observation age and upstream quote activity are different.
- What the history index helps and what it costs.
- What changes if collection takes longer than the polling interval.

For each milestone, make one small change yourself, run it, and explain the result. Keep brief design notes with the problem, chosen approach, alternative, and observed evidence.

## 11. Future versions

1. Watchlist management and threshold-crossing alerts.
2. Paper portfolio and timestamped trade journal, with safe retries and concurrent balance updates.
3. Polymarket adapter and manually reviewed equivalent-contract mappings.
4. Import forecast outputs from the separate Premier League project.
5. Historical replay, measured performance tuning, and streaming only when justified.

These are roadmap items, not requirements for the first release.

## 12. Initial Codex prompt

```text
Read Prediction_Market_Engine_PRD.md as the specification for this repository.
We are building a small working backend quickly, and I want to understand it.
Follow section 10's learning workflow throughout implementation.

Inspect the repository and any AGENTS.md first. Preserve existing work.
Start with milestone 1, step 1A only: fetch and inspect one raw Kalshi market
and order-book response. Verify the current official API documentation.

Before coding, briefly explain the goal, inputs, outputs, proposed files, and
any essential new concept. Explain why each new dependency is needed.
Implement only this step with straightforward code and explicit timeouts.
Do not generate the remaining milestone or future features in advance.

Run the code when network access permits and clearly state whether a live
request succeeded. Label fixture data explicitly if used. Give exact commands.
Trace one concrete response through the important functions and explain the
relevant syntax and one design tradeoff.

Give me one small modification to make myself and two understanding questions.
Wait for my answer or explicit instruction to continue before the next step.
At completed milestone boundaries, run the relevant tests and suggest a commit.
```

Continuation prompt after each small step:

```text
Continue with the next small step in section 10 of
Prediction_Market_Engine_PRD.md. Check my answers if I supplied them and clarify
any misunderstanding first. Preserve existing working code and implement only
this step. Explain, run, and trace it; give me one modification and two questions.
Pause at the learning checkpoint. Report a milestone as complete only after
its acceptance criteria and relevant tests pass.
```

## 13. Official references

Verified during project planning on October 9, 2026 (America/Los_Angeles). Recheck API fields during implementation.

- Kalshi public market data: https://docs.kalshi.com/getting_started/quick_start_market_data
- Kalshi order-book semantics and fixed-point fields: https://docs.kalshi.com/getting_started/orderbook_responses
- PostgreSQL locking (relevant to later paper trading): https://www.postgresql.org/docs/current/explicit-locking.html


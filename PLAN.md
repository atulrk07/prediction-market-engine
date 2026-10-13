# Implementation plan and review decisions

Reviewed October 11, 2026 (America/Los_Angeles). Future entries are planning
decisions; the current-evidence section identifies implemented, verified work.

## Sources and scope

The repository-local [PRD](Prediction_Market_Engine_PRD.md) preserves the original
specification, including section 10's learning workflow. This plan supplements
it with the attached `Prediction_Market_Project_Summary_and_Improvements.docx`
and `AGENTS-prediction-market-engine.md`. The local [AGENTS.md](AGENTS.md) adapts
the supplied guidance and restores the learning workflow. The planning review
authorized documentation changes only. Atul subsequently
requested continuation through 2A; implementation now pauses at the 2A checkpoint.

Where the PRD is ambiguous, use the clarifications below; they do not replace
its remaining requirements. Explicit user instructions take precedence over
all project documents.

Keep the four milestones and existing stack. First release: **Prediction Market
Data Pipeline and API**, following 5–10 configured Kalshi binary markets with
PostgreSQL history and a read-only FastAPI API. Start synchronous, with one
sequential worker. GET requests read the database without contacting Kalshi.
Dashboards, alerts, paper trading, Polymarket, streaming, public hosting, and
multi-worker coordination remain later work.

## Current evidence

- Step 1A: implemented and live-verified October 9, 2026; market and order-book
  requests both returned HTTP 200. See the README for the concrete response.
- Commit `960cea0` was pushed to GitHub `main` for step 1A.
- Commit `4961f38` records the remaining milestone 1 work. Step 2A is saved in
  a separate schema checkpoint commit; consult git log for that revision.
- Atul answered the revised 1A understanding questions correctly. His formatting
  modification was not confirmed. He answered the simplified 1B questions
  correctly (50-cent ask, not crossed). The 1B fixture modification was not
  confirmed; explicit continuation authorized 1C.
- Step 1B: live-verified October 11, 2026; both endpoints returned HTTP 200
  for an active binary market. Manual normalization and rejection checks passed.
  Price precision is four decimal places and quantity precision two, based on
  current official fixed-point documentation. See README for output and checks.
- Step 1C: 85 offline tests passed; editable package/commands installed; live
  discovery returned three active binary candidates across two HTTP 200 pages.
  A discovered active market's quote also fetched successfully with a two-sided
  book. See README for the dated trace and output. Milestone 1's technical
  acceptance checks passed. Atul cleared the simplified 1C understanding
  checkpoint (cursor continuation and 6 maximum markets at 2/page × 3 requests).
- Step 2A: applied migration 0001 to real PostgreSQL 18.6 on October 12. All
  three app tables are empty; 97 tests passed with real schema checks enabled.
  See docs/step_2a.md for commands and the pending learning checkpoint.
- Docker is absent, so Compose startup is unverified. A temporary native
  PostgreSQL verification cluster is running on localhost:5433. Unscaled exact
  NUMERIC plus checks preserves supported precision without silently rounding.
- No application ingestion writes, API, worker, metrics report, or benchmark
  is implemented. Committing step 2A does not complete milestone 2 or its
  pending learning checkpoint.

## Changes assigned to existing steps

| Step | Plan and acceptance evidence |
| --- | --- |
| 1A | Complete: fetch and inspect raw responses. Preserve the working script. |
| 1B | Implemented and verified; Atul answered simplified questions correctly. Validate required fields and fixed-point strings, normalize with Decimal, preserve empty sides, and flag crossed quotes. |
| 1C | Implemented, verified, and understanding checkpoint cleared. Bounded discovery, modules/commands, 85 offline tests, and a live normalized quote satisfy the milestone's technical acceptance checks. |
| 2A | Implemented and migration 0001 applied to real PostgreSQL; learning checkpoint pending. Tables include run attribution/configuration/outcomes, exact numerics and constraints. App tables remain empty; 97 tests pass. |
| 2B | Save/query one observation. Commit metadata and snapshot together per market. Persist the originating run ID; use committed rows as evidence of inserts. Prove durability. |
| 2C | Add database conflict handling without replacing the first valid snapshot. Test sequential and competing inserts, unchanged-price new slots, and per-market rollback isolation against PostgreSQL. |
| 3A | Implement health/readiness and market routes. Historical markets remain queryable regardless of watchlist membership. |
| 3B | Define explicit known-without-data, empty-book, crossed-book, and stale responses. Include market status separately from observation status. Test freshness with a controlled clock and verify no exchange calls. |
| 3C | Implement bounded UTC history with stable ordering, decimal strings, 7-day range and 1,000-row caps. Test limits and invalid input; verify the manually refreshed pipeline end to end. |
| 4A | One separate sequential worker; watchlist eligibility and closed-market handling; skip missed slots, retain history, and record actual timing. |
| 4B | At most three HTTP attempts per request; explicit phase timeouts, cycle/retry budgets, Retry-After handling, outcome tracking, honest interrupted runs, and controlled failure tests. |
| 4C | Compose, migrations before startup, named volume, localhost demo, offline CI, and evidence from several real cycles plus a controlled failure/stale-data demo. |

This table expands acceptance details within the existing steps. It does not
authorize implementing them ahead of their learning checkpoints.

## Data validation decisions

An empty array is valid absence of liquidity. A missing side, null side, wrong
shape, invalid value, or unsupported precision is a validation failure. Do not
use a default empty list for a missing required key. Reject the entire malformed
observation with a reason rather than silently dropping bad levels and calling
the resulting book complete. Valid zero-sized levels are ignored.

Select supported price/quantity precision from current official fixed-point
documentation during 1B; carry the same limits into PostgreSQL in 2A. Reject
excess precision without rounding. Compute maxima explicitly instead of relying
on ordering. Preserve original values; never convert monetary strings through
a float.

A crossed quote means YES bid exceeds the derived YES ask. Retain the observed
values and negative spread, and flag the result for review; do not clamp or
swap prices. Derive the flag from stored prices rather than adding a separate
database column. Report crossed observations separately from usable quotes.

`observed_at` is the timezone-aware UTC time the book response was received.
`collection_slot` identifies the intended collection cycle and stays unchanged
through retries. `source_timestamp` is nullable and only comes from an explicit,
documented timestamp for the source observation; do not reuse market metadata's
`updated_time` as an order-book event timestamp.

## Database and run evidence

Retain unique `(platform, external_id)` for markets and unique
`(market_id, collection_slot)` for snapshots. Insert snapshots with targeted
`ON CONFLICT (market_id, collection_slot) DO NOTHING`; a replay keeps the
existing snapshot. A new slot with the same prices is new history.

Add `ingestion_run_id` to snapshots as a foreign key to the run that actually
inserted them. Create a run before its writes. This attribution allows inserted
counts to be rebuilt from committed rows even when a process crashes before
updating summary counters. A duplicate attempt retains the snapshot's original
run attribution. Keep each market's metadata upsert and snapshot insert in one
transaction; roll back storage failures before proceeding to another market.

Plan small JSON fields on `ingestion_runs` for watchlist/interval configuration
and per-market outcomes: `inserted`, `duplicate`, `fetch_failed`,
`validation_failed`, `storage_failed`, and `not_attempted`. The attached lists
are examples; `storage_failed` closes the gap for database errors. Capture
concise reasons and timing, without credentials. Cached counts must not be
treated as stronger evidence than committed rows.

A running record without finalization remains incomplete for reporting after
interruption. Do not infer success from elapsed time or invented outcomes;
unknown outcomes stay unknown. Finalized runs distinguish success, partial
failure, and failure. Document exact transitions and count definitions in 4B.

The extra run attribution and JSON fields add a small amount of write/storage
work but make crash reporting verifiable without a monitoring service.

## Tracking and API decisions

The configured watchlist controls collection; stored market rows and snapshots
control historical queries. Removing a ticker from the watchlist or closing a
market must not delete history. Refresh metadata for configured markets; when
confirmed closed/settled, stop routine book polling while retaining status and
history. Record eligibility changes for any measured collection window.

Keep the PRD's endpoint and pagination limits. For latest quote:

- Unknown market: HTTP 404.
- Known market with no snapshots: HTTP 200, `status: "no_observations"`,
  `quote: null`. No invented observation time, price, or age.
- Known market with a snapshot: HTTP 200, snapshot fields and nullable prices,
  market status, collection age/stale flag, and derived crossed flag.
- A valid empty snapshot is an observation; it differs from no snapshots.
  Staleness describes collection age independently of liquidity or market status.

Choose precise response model field names during 3B. Keep exchange market status
separate from the response's observation status to avoid overloading one field.

## Scheduling and timeout decisions

The 60-second interval is a scheduling target. Each cycle retains its intended
UTC slot; per-market observation times can differ. Track elapsed budgets with a
monotonic clock and timestamps with UTC wall time. Record cycle overruns and
the delay between a slot and each actual observation.

Request phase timeouts and a cycle budget solve different problems. Before
starting another request/attempt or retry wait, check remaining budget. Respect
a valid Retry-After (including its documented formats); if it exceeds remaining
budget, stop that request for this cycle instead of retrying early. Budget
exhaustion leaves unvisited markets `not_attempted`. A synchronous request
already in flight cannot be forcibly stopped by these budget checks; document
that limitation and choose concrete budget values during 4B.

Skip elapsed slots after an overrun or restart; do not invent past observations
or run catch-up bursts. Run the worker separately from FastAPI startup so API
server processes do not each launch a collector. An optional singleton lock
is deferred until accidental second workers are a demonstrated concern.

## Measurement policy and release gates

Required release evidence remains small: dated live smoke check, PostgreSQL
replay/concurrency verification, controlled failure and stale-data demo,
several real worker cycles, passing relevant tests, and reproducible startup
from a clean checkout. Save the commit and configuration with evidence.

A reporting command, sustained coverage study, and load benchmark are follow-up
work after the working backend. They are prerequisites for numerical resume
claims, not extra gates for implementing 1B or finishing the functional release.
Until measured, leave quantities, coverage, latency, and uptime claims unset.

When measuring, save a fixed window `[start, end)`, watchlist eligibility,
interval, software commit, and environment:

- Expected observations are eligible market-slot pairs across the full window,
  including downtime. A run log alone cannot supply this denominator.
- Eventual coverage counts unique valid stored observations for those pairs;
  duplicate retries do not increase it.
- On-time coverage additionally requires observation before the next scheduled
  slot. Report late observations and cycle overruns separately.
- Count committed live snapshots and distinct markets; exclude fixtures and
  synthetic benchmark records. Use isolated test/benchmark data stores.
- Report two-sided noncrossed quote share separately: a valid empty book can
  count as collected data without providing a usable quote.
- Report run outcomes separately from coverage. Successful attempted runs do
  not establish uptime or reveal cycles that never started.
- For API benchmarks, retain workload, raw samples, errors/timeouts, request
  count, concurrency, row count, hardware, worker state, p50/p95, and warmup.
  Any index comparison must use the same workload and actual before/after data.

Milestones 1–3 yield a manually refreshed pipeline/API. Milestone 4 completes
scheduled collection and release packaging. Future alerts describe observed
price movement unless a fair-value model is independently built and validated.

## Official references checked in this review

- [Kalshi book semantics](https://docs.kalshi.com/getting_started/orderbook_responses)
- [PostgreSQL INSERT/conflict handling](https://www.postgresql.org/docs/current/sql-insert.html)
- [HTTPX timeout phases](https://www.python-httpx.org/advanced/timeouts/)

These references support technical behavior; our budgets, outcome schema,
crossed-book policy, and coverage definitions are project design choices.

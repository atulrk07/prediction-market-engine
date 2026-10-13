# AGENTS.md — Prediction Market Engine

Context for Codex before continuing work. Read this fully before editing.

## Specification and current checkpoint
- User instructions take precedence. The original specification is
  `Prediction_Market_Engine_PRD.md`; `PLAN.md` records the accepted planning
  clarifications from the October 11, 2026 document review. Read both before
  implementing the next step. Roadmap suggestions are not current build tasks.
- Steps 1A–1C are implemented and live-verified. Atul answered the revised 1A
  and simplified 1B questions correctly; his fixture/formatting modifications
  were not confirmed. Milestone 1's technical acceptance checks passed (85
  offline tests plus a live active-market quote and paginated discovery).
  Atul cleared the simplified 1C checkpoint (cursor continuation and 6 markets
  maximum at 2/page × 3 requests). Step 2A is implemented and revision 0001
  applied to real PostgreSQL 18.6; 97 tests passed. See docs/step_2a.md.
  The database tables are empty; app ingestion is not implemented. Docker is
  absent; a temporary native verification cluster is running on localhost:5433.
  Step 2A awaits its learning checkpoint. Do not start 2B before his answer or
  explicit continuation. Milestone 1 is committed as 4961f38; step 2A is saved
  in a separate schema checkpoint commit. Consult git log for its revision.

## Learning workflow (PRD section 10)
- Complete only one small runnable step per implementation checkpoint. Preserve
  existing work; do not generate later steps or milestones in advance.
- Before coding, explain the goal, inputs, outputs, proposed files, essential
  concepts, and why each new dependency is needed.
- After coding, run relevant checks, give exact commands, state whether live
  access succeeded, and clearly label fixtures. Trace one concrete response
  through the important functions, explaining relevant syntax and one tradeoff.
- Give Atul one small modification and two understanding questions. Wait for
  his answer or explicit instruction before proceeding; clarify misconceptions.
- Explain directly in chat; a README link does not replace the walkthrough.
- Run milestone acceptance tests and suggest a commit at completed milestone
  boundaries. Honor explicit requests to commit earlier. Describe completion
  only after the required behaviors are verified.

## What this project is
A small, reliable market-data backend. A worker collects Kalshi quotes, normalizes them, and
stores historical observations in PostgreSQL; FastAPI serves the stored records. Repo name:
`prediction-market-engine`. Describe the first release as **"Prediction Market Data Pipeline and
API."** This is a data backend — it does NOT judge mispricing, recommend trades, or predict.

## Architecture (keep this boundary)
- Separate **worker** fetches + validates + commits observations. FastAPI only **reads** from the DB.
- **An API read must never trigger an exchange request.** Stored data stays available during an
  upstream outage; expose its collection age/staleness.
- Stack: Python, HTTPX, Pydantic, SQLAlchemy, Alembic, FastAPI, pytest, Docker Compose, GitHub
  Actions. Start synchronous, one sequential worker.

## KEEP — the design decisions that make this project credible
1. **Atomic dedup at the DB level:** unique (market_id, collection-slot) constraint +
   `INSERT ... ON CONFLICT DO NOTHING`. An app-level check-then-insert has a race; do not revert to it.
2. **Idempotent/replay-safe writes:** a replay must not overwrite an existing snapshot; keep the
   slot's first valid observation. An unchanged price in a *new* slot is new history.
3. **Decimal correctness:** use `Decimal` and exact DB numerics for prices. Return decimals as JSON
   strings. No floats for money.
4. **Time semantics kept distinct:** scheduled slot vs. actual collection time vs. exchange event
   time. Only store an exchange timestamp when one is supplied.
5. **Failure isolation:** one market failing must not abort the run; roll back that market's
   transaction and continue.

## FIX — apply before calling the first release complete
**1. Distinguish malformed data from an empty book.** Require the expected top-level object and
side arrays; reject missing keys, bad shapes, nonfinite numbers, unsupported precision. Accept
valid empty arrays as "no liquidity." Prevents a schema change from silently becoming an empty
observation. Test both; flag crossed quotes for review.

**2. Atomic duplicate handling.** Use the unique constraint + ON CONFLICT DO NOTHING (not an
app-only check). Commit each market's metadata + snapshot together; roll back on error. Test
concurrent inserts and sequential retries.

**3. Budget collection vs. retry time separately.** The 60s interval is a target, not a guarantee.
HTTPX timeouts cover connect/read/write/pool — a read timeout is not a total deadline. Track
elapsed cycle time, cap retry attempts/waits, skip missed slots (never backfill invented
observations), respect `Retry-After`. Document that a cycle budget can't cancel an in-flight sync request.

**4. Run records must explain failures.** Per-market outcomes: inserted / duplicate /
fetch_failed / validation_failed / not_attempted (a small JSON field on `ingestion_runs` is
enough). Mark crashed/unfinished runs **incomplete, never successful**. Derive row counts from
committed DB rows so a crash can't inflate success.
Also record `storage_failed` for a transaction failure; do not label it as a
fetch or validation failure. Link snapshots to their originating run as planned
in `PLAN.md` so committed insert counts can be reconstructed after a crash.

**5. Market state + empty responses.** Separate active tracking from historical records; preserve
snapshots when a market closes. Return **404 for unknown market**; **200 with status
`no_observations` and quote null** for a known market with no data; otherwise include status +
stale flag.

**6. Deployment boundary.** Run ONE worker outside FastAPI startup hooks (avoid duplicate polling
across server processes). Apply migrations before startup; named DB volume; bind demo locally.
Defer public hosting and multi-worker coordination.

## Quote semantics (get this exactly right)
Kalshi book has YES and NO bids as fixed-point strings. Derive **YES ask = 1 − best NO bid**,
using its quantity. Example: bids YES 0.42×10 and NO 0.55×8 → YES ask 0.45×8, spread 0.03. Use
Decimal throughout. Empty sides stay null; a missing *required* field is a validation error, not
an empty book.

## API surface
Index `market_id`, `observed_at`, `id`. Limit responses to 100 markets / 1,000 history rows, 7-day
max range. Decimals as JSON strings. Distinguish unknown markets from markets with no observations.

## HARD RULES (non-negotiable)
- **Milestones are completion criteria, not claims.** Only describe completed, verified checkpoints.
- **No invented data.** Skip missed slots; never backfill fabricated observations. Label any
  synthetic/benchmark data as synthetic and exclude it from live-ingestion counts.
- **Metrics stay blank until measured.** The coverage/latency resume numbers are placeholders until
  real artifacts exist (live smoke result, metrics report, duplicate-replay output, one controlled
  failure demo, reproducible startup command).
- Movement ≠ mispricing: a low price alone does not establish mispricing or positive EV. Any future
  alert is a "movement alert" unless a validated fair-value model is supplied.

## Tests that matter
Correct ask/quantity mapping; missing vs. malformed; decimal precision; one row for
repeated/concurrent inserts; a new row for a new unchanged-price slot; failure isolation; bounded
retries; ordered+bounded history; stale-state transitions; DB-only API reads. PostgreSQL for
integration tests, mocked HTTP in CI.

## Must be able to explain (build so these answers are true)
- Trace one response: validation → normalization → DB transaction → API response.
- Why a unique constraint is needed even when Python checks for duplicates.
- Scheduled slot vs. actual collection time vs. source event time.
- What happens when one market fails, the worker stops, or a request asks for too much history.
- Purpose and write-cost of the history index; why one worker is enough initially.

## Resume claims this supports (don't exceed them)
- After milestones 1–3: "Built a Python and FastAPI service to normalize Kalshi order-book data
  and query historical snapshots in PostgreSQL, using transactional writes, database-enforced
  deduplication, and indexed history queries."
- After measurement only: "Collected [Y] live snapshots across [X] markets over [D] days with
  [Z]% on-time coverage; served [endpoint] at [L] ms p95 under [C] concurrent clients on
  [environment]." Keep only numbers you measured and can explain.

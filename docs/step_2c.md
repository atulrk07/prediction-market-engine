# Step 2C: keep repeats from adding duplicate snapshots

Verified October 13, 2026 (America/Los_Angeles). Snapshot replay is safe, real
PostgreSQL concurrency tests pass, and data survives a server restart. Milestone
2's technical acceptance checks are satisfied. Atul subsequently cleared the
learning checkpoint (one snapshot for a repeat, another for a new slot), and
his display modification below is present in replay_quote.py.

## The rule

A snapshot is identified by market_id plus collection_slot. Repeat the same
market/slot and keep the first snapshot. A later slot is a new observation,
even if its price is unchanged. Replays preserve all original snapshot fields,
including observed_at and ingestion_run_id.

PostgreSQL's existing unique constraint enforces this identity. The snapshot
INSERT now uses targeted ON CONFLICT DO NOTHING, so a duplicate is handled rather
than raised as a uniqueness error. Invalid values, foreign-key failures, and
other database errors still fail. No migration or new dependency was needed.

## Files and commands

- persistence.py returns snapshot_id, run_id, and outcome (inserted/duplicate).
  The metadata upsert, snapshot action, and finalized run record remain in one
  transaction. No application check-then-insert is used.
- ingest_quote.py reports the attempt's outcome and returns stored snapshot data.
  attempt_run_id identifies this attempt; ingestion_run_id identifies the run
  that actually inserted the snapshot. They differ on a replay.
- replay_quote.py reads a saved observation and retries its write using its
  original slot and quote values. It makes no HTTP request and cannot create
  invented missing-slot history. It errors if the source ID does not exist.
- Tests cover sequential repeats, competing full writes, competing snapshot
  inserts without metadata serialization, unchanged prices in a new slot, and
  failure isolation across markets. Test data is synthetic and isolated.

With the database running and migration 0001 applied:

```sh
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python replay_quote.py 1
.venv/bin/python replay_quote.py 1
.venv/bin/python read_quote.py 1
.venv/bin/python ingest_quote.py KXHIGHNY-26OCT14-B84.5
```

Use your saved ID and a current discovered ticker when needed. Installed replay
command: .venv/bin/kalshi-replay. The other commands retain their entry points.

## Concrete live-data replay trace

Snapshot 1 was collected live in 2B for KXHIGHNY-26OCT14-B84.5. Its slot was
2026-10-14T01:04:00+00:00; observation time was 01:04:26.556202Z; YES bid was null,
YES ask was "0.0100", and ask quantity was "6301.85".

1. replay_observation() reads snapshot 1 and parses its saved JSON back into a
   Quote with exact Decimal values and aware timestamps. It retains the old slot.
2. save_observation() creates attempt run 2 and obtains the existing market ID 1.
3. The snapshot INSERT encounters the existing (1, 01:04 slot) key. PostgreSQL
   does not insert or update that snapshot, so RETURNING gives no new ID.
4. scalar_one_or_none() returns None. The code selects the existing snapshot ID
   after PostgreSQL has resolved the conflict; this is not a pre-insert check.
5. The attempt is finalized as success with inserted_count=0 and duplicate_count=1.
   The result is {snapshot_id: 1, run_id: 2, outcome: "duplicate"}.
6. A second replay creates run 3, also duplicate. Snapshot 1 still belongs to run
   1, with identical original timestamps, price, and quantity.

Actual database results after both replays:

| Run | Inserted | Duplicate | Snapshot affected |
| --- | --- | --- | --- |
| 1 (original) | 1 | 0 | Created snapshot 1 |
| 2 (replay) | 0 | 1 | Kept snapshot 1 |
| 3 (replay) | 0 | 1 | Kept snapshot 1 |

There was still one snapshot. A later live collection then returned HTTP 200
from both Kalshi endpoints for the active market. Run 4 inserted snapshot 4 in
slot 2026-10-14T02:10:00+00:00, observed at 02:10:45.013872Z. The ask remained
"0.0100"; quantity was "8724.85". This is new history despite the unchanged price.
The UTC date is October 14; the Los Angeles date is October 13. No missed slots
were filled. IDs can have gaps because ignored inserts may consume sequence
values; IDs identify rows, not the number of stored observations.

## Syntax and design tradeoff

```python
.on_conflict_do_nothing(constraint="uq_snapshots_market_slot")
.returning(snapshots.c.id)
```

The named constraint selects exactly the duplicate identity we handle. RETURNING
returns a new ID only if a row was inserted. scalar_one_or_none() reads that ID
or None, which lets us label the attempt inserted or duplicate. The database
decides atomically, even when two writers start together. A Python precheck
could let both writers see no row before either inserts.

Each attempt gets a run record; that is separate from the one retained snapshot.
The snapshot's original run ID never changes. Counts come from stored rows for
this run; duplicates do not inflate inserted counts. As in 2B, interrupted
transactions leave unfinished run records; fuller failure reporting belongs to 4B.

Tradeoff: first-valid-observation wins gives one consistent record per slot.
A different price arriving in a repeat of the same slot does not replace it.
This history therefore samples observations rather than preserving every
within-slot price movement.

## PostgreSQL durability and verification

The native data directory was copied while PostgreSQL was stopped, then the
server restarted from .local/postgres-data. The original /tmp directory was
preserved as a backup. Existing snapshot 1 remained readable after restart.
The new directory is ignored by Git, and contains both app and test databases.
The app's actual live data was preserved; no database was reset.

The current server listens on 127.0.0.1:5433. Native PostgreSQL binaries are still
on the mounted official Postgres.app image, so remounting/reinstalling that
runtime may be needed after reboot. Data files are now in the project, not /tmp.
Docker Compose startup remains unverified; the Compose service defines its own
named volume. Switching runtimes does not automatically transfer native data.

Current native stop/start commands from the repository root:

```sh
/tmp/prediction-market-postgres-image/Postgres.app/Contents/Versions/18/bin/pg_ctl -D .local/postgres-data -w -t 10 stop
/tmp/prediction-market-postgres-image/Postgres.app/Contents/Versions/18/bin/pg_ctl -D .local/postgres-data -l .local/postgres-data/server.log -o '-h 127.0.0.1 -p 5433 -k /Users/atulkamath/Desktop/prediction-market-monitor/.local/postgres-data -c timezone=UTC' -w -t 10 start
```

Keep the native server stopped when using Compose on the same port. Use the
appropriate DATABASE_URL for whichever database actually contains your history.

Milestone test command:

```sh
TEST_DATABASE_URL='postgresql+psycopg://prediction_market:local_dev_password@127.0.0.1:5433/prediction_market_test' .venv/bin/python -m pytest -q
```

All 111 tests passed on real PostgreSQL 18.6, including independent concurrent
clients and a barrier-synchronized direct snapshot race. Tests use bounded
barrier/DB timeouts and remove their own UUID-tagged synthetic rows. Offline
HTTP stays mocked/blocked. Milestone 2 verifies saved history, replay, new slots,
rollback/isolation, and reads after restart. API and worker code are not built.
Suggested milestone commit: feat: persist idempotent market snapshots in PostgreSQL.

## Learning checkpoint

Your modification: in replay_quote.py replace:

```python
print(json.dumps(result, indent=2))
```

with:

```python
print(result["outcome"])
```

Run `.venv/bin/python replay_quote.py 1` twice. Both should print duplicate;
reading snapshot 1 still shows its original observation. This changes display.

1. If the same market and 12:00 slot are written twice, how many snapshots remain?
2. If the price is unchanged in the 12:01 slot, should we store another snapshot?

This checkpoint is cleared. Step 3A was subsequently explicitly authorized;
see docs/step_3a.md for the current checkpoint.

References checked:

- [PostgreSQL INSERT/conflicts](https://www.postgresql.org/docs/18/sql-insert.html)
- [SQLAlchemy PostgreSQL inserts](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)

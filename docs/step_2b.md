# Step 2B: save an observation and read it back

Verified October 13, 2026 (America/Los_Angeles). A live observation was committed
to PostgreSQL and read in a separate Python process. The understanding checkpoint
was cleared by Atul (ingest/read and transaction rollback). See [2C](step_2c.md)
for current replay behavior and the current learning checkpoint.

## Goal, inputs, and outputs

Before this step, a quote disappeared from our program when it exited. Now the
collector commits a market row, a snapshot, and a collection-run record. A
separate command reads the snapshot by ID without requesting Kalshi again.

Inputs are a ticker, live metadata/book responses, and DATABASE_URL. Outputs are
the committed snapshot ID and JSON read from PostgreSQL. No dependency or schema
migration was added. Prices/quantities stay Decimal in Python and NUMERIC in
PostgreSQL; printed JSON uses strings, and absent values stay null.

## Files and commands

- fetch_quote.py exposes fetch_live_quote() for both quote printing and ingestion.
- persistence.py maps metadata, saves an observation, and reads one by ID.
- ingest_quote.py is a one-shot live fetch/save/read command; it has no fixture
  write mode. Tests use synthetic data only in the isolated test database.
- read_quote.py reads PostgreSQL only and returns an error for a missing ID.
- tests/test_persistence.py verifies real PostgreSQL commits, reads, metadata
  reuse, empty sides, and rollback. Shared test setup checks the _test DB suffix.

With the existing PostgreSQL server running and revision 0001 applied:

```sh
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python discover_markets.py --series KXHIGHNY --limit 3
.venv/bin/python ingest_quote.py KXHIGHNY-26OCT14-B84.5
.venv/bin/python read_quote.py 1
```

Choose a current discovered ticker if that example has closed, and use the ID
printed by your collection. Installed equivalents are kalshi-ingest and
kalshi-read under .venv/bin. The old fetch_raw/fetch_quote commands still work.
The native runtime and standard Compose setup are documented in [2A](step_2a.md).

## Concrete live trace

1. ingest_quote.main() chose the UTC minute slot 2026-10-14T01:04:00+00:00 before
   requesting Kalshi. It is the identity of this manual collection, not the
   timestamp of the upstream orders.
2. fetch_live_quote() fetched KXHIGHNY-26OCT14-B84.5 metadata and order book.
   Both requests returned HTTP 200; the returned market status was active.
3. normalize_orderbook() produced an absent YES bid and a YES ask of
   Decimal("0.0100") with quantity Decimal("6301.85"). Actual observation time
   was 2026-10-14T01:04:26.556202Z. With no YES bid, spread stayed None.
4. save_observation() validated metadata, created run 1, and started the market
   transaction. The metadata upsert returned market ID 1. The snapshot insert
   used market_id=1 and ingestion_run_id=1 and returned snapshot ID 1.
5. The transaction recorded one inserted snapshot, finalized run 1 as success,
   and committed. The three application tables each contained one row.
6. read_observation() selected snapshot 1 joined to its market. Decimal values
   came back from PostgreSQL; the Quote model serialized them as strings.
7. A separate read_quote.py process returned the same saved values below. It
   made no Kalshi request and did not invent a new observation time.

This is real live data, not a fixture. The UTC timestamp's date is October 14;
its Los Angeles date is October 13.

```json
{
  "snapshot_id": 1,
  "market_id": 1,
  "ingestion_run_id": 1,
  "collection_slot": "2026-10-14T01:04:00+00:00",
  "title": "Will the maximum temperature be 84-85° on Oct 14, 2026?",
  "market_status": "active",
  "close_time": "2026-10-15T05:00:00+00:00",
  "quote": {
    "ticker": "KXHIGHNY-26OCT14-B84.5",
    "observed_at": "2026-10-14T01:04:26.556202Z",
    "source_timestamp": null,
    "yes_bid": null,
    "yes_bid_quantity": null,
    "yes_ask": "0.0100",
    "yes_ask_quantity": "6301.85",
    "spread": null,
    "is_crossed": false
  }
}
```

## Transactions and relevant syntax

A transaction groups writes so they commit together or roll back together.
`with engine.begin() as connection:` opens that group; normal exit commits,
and an exception rolls it back. Market metadata, snapshot, and the run's success
update use the same transaction. A rejected snapshot cannot leave a changed
market title or a false successful run behind.

The initial running record is committed separately so an interrupted write has
an unfinished record. In this step, a failed storage transaction leaves that
record running with no finished_at and zero inserted rows; treat it as incomplete,
not successful. Full failure classification/recovery reporting belongs to 4B.
The success counter is counted from the snapshot rows in the transaction and
commits atomically with them.

`insert(markets).values(**values)` builds an INSERT; **values unpacks the
dictionary into named fields. `.on_conflict_do_update(...)` is a metadata upsert:
reuse the existing market identified by platform/ticker and refresh its fields.
It does not overwrite snapshots. `.returning(markets.c.id)` asks PostgreSQL to
return the ID; `.scalar_one()` reads that one value. `markets.c.id` refers to
the table's id column. SELECT with JOIN connects snapshots to market metadata;
`.mappings()` exposes the returned row as dictionary-like named fields.

The command uses a 60-second slot; minute_slot() converts to UTC and removes
seconds/microseconds. Book collection time stays exact and separate. At this
historical 2B checkpoint, repeats raised a uniqueness error. Step 2C now handles
them safely and returns a structured save outcome. Use read_quote.py to inspect
a saved row without collecting again.

Design tradeoff: fetch and validation complete before the database transaction,
so upstream waits do not hold database locks. HTTP failures currently create no
run record; comprehensive collection-failure tracking comes with the worker.
Metadata read alongside a historical quote is the market's current stored
metadata, not a separate historical copy of its title/status.

## Verification and current limits

```sh
.venv/bin/python -m pytest -q
TEST_DATABASE_URL='postgresql+psycopg://prediction_market:local_dev_password@127.0.0.1:5433/prediction_market_test' .venv/bin/python -m pytest -q
```

The first command passed 90 offline checks and explicitly skipped 15 PostgreSQL
checks when no test URL was supplied. The second passed all 105 tests on real
PostgreSQL. Tests commit UUID-tagged synthetic observations in the test DB and
remove only their own rows afterward. A new-engine read proves the result is
committed; a rejected write proves metadata rollback and an unfinished run.
Failed mocked HTTP never opens the database. Installation/help/dependency checks
passed. The live collection and separate-process read both exited 0.

At the 2B checkpoint, the native data directory was temporary. Step 2C preserves
it in .local/postgres-data and verifies restart persistence. Compose defines a
named volume; Docker startup is still unverified because Docker is absent.
There is no API, scheduled worker, or HTTP retry policy yet.

## Learning checkpoint

Your modification: in read_quote.py change:

```python
print(json.dumps(result, indent=2))
```

to:

```python
print(json.dumps(result["quote"], indent=2))
```

Then run `.venv/bin/python read_quote.py 1`. It should print only the stored
quote fields, without the outer IDs/title. This changes display, not saved data.
Use your own saved ID if snapshot 1 is unavailable.

1. If the snapshot insert fails, should the market's title update remain saved?
   What does the transaction do?
2. If you run read_quote.py tomorrow, will it fetch fresh Kalshi prices or return
   the saved observation? Which timestamp should remain unchanged?

Atul cleared this checkpoint. The current checkpoint is in the linked 2C guide.

References checked October 13:

- [SQLAlchemy transactions](https://docs.sqlalchemy.org/en/20/tutorial/dbapi_transactions.html)
- [PostgreSQL metadata upserts](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)
- [Kalshi market response](https://docs.kalshi.com/api-reference/market/get-market)
- [Kalshi book semantics](https://docs.kalshi.com/getting_started/orderbook_responses)

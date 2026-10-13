# Step 2A: create the database structure

Verified October 12, 2026 (America/Los_Angeles). Step 2A is implemented and
applied to real PostgreSQL 18.6. The learning checkpoint is pending. No
application ingestion writes, API, or worker have been implemented.

## What this step does

A table is a collection of rows with named columns, like a spreadsheet with
enforced rules. We created three empty tables:

| Table | Purpose |
| --- | --- |
| markets | One row of metadata for each tracked market |
| snapshots | Observed prices/times, linked to a market and the run that inserted them |
| ingestion_runs | Collection attempts, their status/counts, and configuration/outcomes |

A primary key identifies a row (`id`). A foreign key references another table's
primary key (`snapshots.market_id` references `markets.id`). A unique constraint
prevents two rows with the same identity: snapshots use `(market_id,
collection_slot)`, not price, as their identity. Conflict handling comes in 2C.

`collection_slot` is the intended collection time; `observed_at` is when the
book response was actually received. `source_timestamp` stays null unless the
exchange supplies a documented timestamp for that observation.

## Files and dependencies

- models.py describes the current tables using SQLAlchemy's Table/Column API.
- database.py configures PostgreSQL connections. DATABASE_URL overrides the
  local default matching Compose. SQLite is rejected.
- compose.yaml describes a PostgreSQL 18.6 service on localhost:5433 with a
  named data volume. Docker execution has not been verified on this machine.
- alembic.ini and migrations/env.py configure Alembic; revision 0001 is the
  permanent initial schema change in migrations/versions/0001_initial_tables.py.
- tests/test_postgres_schema.py verifies the applied schema on real PostgreSQL.
  Synthetic prerequisite rows used to test constraints are rolled back.

SQLAlchemy describes tables and sends SQL. Psycopg is the PostgreSQL driver;
psycopg-binary supplies its compiled client libraries without a system libpq
installation. Alembic applies versioned schema changes; Mako and MarkupSafe
support its migration templates. All installed dependencies are pinned.

A migration is a versioned change to a database. Python table definitions
describe the desired current structure; editing them does not change an
existing database. Future schema changes need a new migration. Keep an applied
migration's history intact instead of rewriting it to change existing databases.

## Standard setup with Docker Desktop

Once Docker Desktop is installed and running:

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
docker compose up -d --wait db
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
.venv/bin/python -m alembic check
docker compose exec db psql -U prediction_market -d prediction_market -c '\dt'
```

The published port binds only to 127.0.0.1. Compose's credentials are local
development defaults, not production secrets. The named volume uses the
PostgreSQL 18 image's /var/lib/postgresql location. Stop the temporary native
cluster described below before using Compose on the same port.

To run schema checks, create and migrate a separate test database:

```sh
docker compose exec db createdb -U prediction_market prediction_market_test
DATABASE_URL='postgresql+psycopg://prediction_market:local_dev_password@127.0.0.1:5433/prediction_market_test' .venv/bin/python -m alembic upgrade head
TEST_DATABASE_URL='postgresql+psycopg://prediction_market:local_dev_password@127.0.0.1:5433/prediction_market_test' .venv/bin/python -m pytest -q
```

Create the test database once; on later runs reuse it. The tests require a
database name ending in _test. They inspect the schema and roll back synthetic
rows rather than writing live ingestion data. Without TEST_DATABASE_URL, the
PostgreSQL tests skip explicitly; the offline tests still run.

## Verification on this machine

Docker and PostgreSQL were initially absent. With approved mount/process access,
we used the PostgreSQL 18.6 binaries from the official Postgres.app 2.9.6 disk
image for an isolated temporary native cluster. The runtime is still running
on localhost:5433; its data directory is /tmp/prediction-market-pg.dNzHHT.
It is temporary verification storage, not the release's durable setup.

Actual results:

- Alembic upgrade head applied revision 0001 successfully to prediction_market.
- PostgreSQL lists markets, snapshots, ingestion_runs, and alembic_version.
- SELECT version_num FROM alembic_version returned 0001.
- All three application tables contain zero rows.
- Alembic check reported: No new upgrade operations detected.
- 97 tests passed with real PostgreSQL enabled (88 offline checks plus 9
  PostgreSQL checks). Synthetic test rows were rolled back.
- Dependency and editable installation checks passed. Compose startup is
  unverified because Docker is not installed.

To inspect the current native cluster:

```sh
/tmp/prediction-market-postgres-image/Postgres.app/Contents/Versions/18/bin/psql -h 127.0.0.1 -p 5433 -U prediction_market -d prediction_market -c '\dt'
.venv/bin/python -m alembic current
.venv/bin/python -m alembic check
TEST_DATABASE_URL='postgresql+psycopg://prediction_market:local_dev_password@127.0.0.1:5433/prediction_market_test' .venv/bin/python -m pytest -q
```

To stop/restart this temporary cluster while its disk image remains mounted:

```sh
/tmp/prediction-market-postgres-image/Postgres.app/Contents/Versions/18/bin/pg_ctl -D /tmp/prediction-market-pg.dNzHHT -w -t 10 stop
/tmp/prediction-market-postgres-image/Postgres.app/Contents/Versions/18/bin/pg_ctl -D /tmp/prediction-market-pg.dNzHHT -l /tmp/prediction-market-pg.dNzHHT/server.log -o '-h 127.0.0.1 -p 5433 -k /tmp/prediction-market-pg.dNzHHT -c timezone=UTC' -w -t 10 start
```

After stopping it, `hdiutil detach /tmp/prediction-market-postgres-image` unmounts
the image. Temporary paths may disappear on cleanup/reboot; the standard Compose
configuration remains in the repository.

## Concrete migration trace and important syntax

1. `.venv/bin/python -m alembic upgrade head` reads alembic.ini and loads env.py.
2. `run_online()` calls `make_engine()`, connects to PostgreSQL, and starts a
   migration transaction. Connections have a five-second connect timeout,
   ten-second SQL statement timeout, and five-second lock wait timeout.
3. `context.run_migrations()` chooses revision 0001 for the initially empty DB
   and calls that file's upgrade(). Its op.create_table() calls issue SQL.
4. For example, the markets id column becomes:

```sql
id BIGINT GENERATED BY DEFAULT AS IDENTITY
```

5. PostgreSQL creates the tables, keys, checks, and history index. Alembic records
   0001 in alembic_version and commits. `run_online()` closes the connection and
   disposes the engine. The table-list response contains all four tables.

`Column("id", BigInteger, Identity(), primary_key=True)` names a column, gives
it an integer type, requests generated IDs, and makes it the row identifier.
`nullable=False` requires a value; `nullable=True` allows SQL NULL.
`ForeignKey("markets.id")` refers to another table's ID. `DateTime(timezone=True)`
uses PostgreSQL TIMESTAMP WITH TIME ZONE. The database stores the instant; the
connection displays timestamps in UTC. `server_default` is applied by PostgreSQL
when an insert omits that column, rather than by the Python script.

Design choice: unscaled exact NUMERIC with checks instead of fixed-scale NUMERIC
prevents silent rounding before precision validation. Price checks enforce 0–1
and at most four decimal places; quantity checks enforce finite nonnegative
values with at most two. The checks add PostgreSQL-specific schema detail but
also protect direct SQL writes. Missing prices/quantities stay paired NULLs.
Crossed prices remain representable; spread/crossed flags are derived later.

The history index on (market_id, observed_at, id) supports selecting one market's
observations in time order. It takes extra storage and work on every write; no
speed improvement is claimed without measurement.

## Learning checkpoint

Your modification: in models.py, change the markets title column's
`nullable=False` to `nullable=True`, then run:

```sh
.venv/bin/python -m alembic check
```

Expect it to report a required schema change and exit nonzero. The existing
database column still requires a title. This command compares; it does not
alter the database. Restore nullable=False and run the same command again;
it should report no new upgrade operations. Do not edit revision 0001 for this
exercise or generate/apply another migration.

1. Which table should hold the market's title, and which should hold many
   price observations over time?
2. Why doesn't editing nullable in models.py immediately change PostgreSQL?

Pause before 2B. Milestone 2 is incomplete; no fetched quote has been saved yet.

Official references checked October 12:

- [SQLAlchemy table definitions](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html)
- [Alembic migrations](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [Psycopg installation](https://www.psycopg.org/psycopg3/docs/basic/install.html)
- [PostgreSQL numeric behavior](https://www.postgresql.org/docs/current/datatype-numeric.html)
- [Official PostgreSQL Docker image](https://hub.docker.com/_/postgres)
- [Postgres.app downloads](https://postgresapp.com/downloads.html)

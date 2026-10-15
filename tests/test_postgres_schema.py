"""Check schema constraints on real PostgreSQL; synthetic rows always roll back."""

from datetime import datetime, timezone
from decimal import Decimal

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import DateTime, Numeric, inspect, text
from sqlalchemy.exc import IntegrityError

from models import ingestion_runs, markets, metadata, snapshots


pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def engine(postgres_engine):
    return postgres_engine


def test_migrations_are_applied(engine):
    assert {"markets", "snapshots", "ingestion_runs", "alert_rules", "alembic_version"}.issubset(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002"


def test_tables_match_sqlalchemy_models(engine):
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
        assert compare_metadata(context, metadata) == []


def test_keys_and_history_index_exist(engine):
    inspector = inspect(engine)
    unique_columns = [tuple(c["column_names"]) for c in inspector.get_unique_constraints("snapshots")]
    assert ("market_id", "collection_slot") in unique_columns
    market_unique = [tuple(c["column_names"]) for c in inspector.get_unique_constraints("markets")]
    assert ("platform", "external_id") in market_unique
    references = {(tuple(c["constrained_columns"]), c["referred_table"]) for c in inspector.get_foreign_keys("snapshots")}
    assert (("market_id",), "markets") in references
    assert (("ingestion_run_id",), "ingestion_runs") in references
    indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("snapshots")}
    assert indexes["ix_snapshots_market_observed_id"] == ["market_id", "observed_at", "id"]


def test_timestamp_and_decimal_storage_types(engine):
    inspector = inspect(engine)
    for table in ("markets", "snapshots", "ingestion_runs", "alert_rules"):
        for column in inspector.get_columns(table):
            if isinstance(column["type"], DateTime):
                assert column["type"].timezone
    columns = {c["name"]: c for c in inspector.get_columns("snapshots")}
    for name in ("yes_bid", "yes_ask", "yes_bid_quantity", "yes_ask_quantity"):
        assert columns[name]["nullable"]
        assert isinstance(columns[name]["type"], Numeric)
        assert columns[name]["type"].scale is None  # No rounding before CHECK.


@pytest.mark.parametrize("field,value,constraint", [
    ("yes_bid", "0.42001", "ck_snapshots_bid_price"),
    ("yes_bid", "0.42000", "ck_snapshots_bid_price"),
    ("yes_ask", "NaN", "ck_snapshots_ask_price"),
    ("yes_bid_quantity", "1.001", "ck_snapshots_bid_quantity"),
    ("yes_ask_quantity", "Infinity", "ck_snapshots_ask_quantity"),
])
def test_database_rejects_bad_precision_and_nonfinite_values(engine, field, value, constraint):
    now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            # Prerequisite rows are synthetic and disappear on rollback.
            market_id = connection.execute(markets.insert().values(
                platform="kalshi", external_id="FIXTURE-SCHEMA", title="Synthetic schema check", status="active",
            ).returning(markets.c.id)).scalar_one()
            run_id = connection.execute(ingestion_runs.insert().values(collection_slot=now).returning(ingestion_runs.c.id)).scalar_one()
            values = dict(market_id=market_id, ingestion_run_id=run_id, collection_slot=now,
                          observed_at=now, yes_bid=Decimal("0.42"), yes_bid_quantity=Decimal("10"),
                          yes_ask=Decimal("0.45"), yes_ask_quantity=Decimal("8"))
            values[field] = Decimal(value)
            with pytest.raises(IntegrityError) as error:
                connection.execute(snapshots.insert().values(**values))
            assert error.value.orig.diag.constraint_name == constraint
        finally:
            transaction.rollback()

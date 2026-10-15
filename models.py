"""PostgreSQL tables, constraints, and indexes for market observations."""

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Identity,
    Index, Integer, MetaData, Numeric, Table, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB


metadata = MetaData()

markets = Table(
    "markets", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("platform", Text, nullable=False),
    Column("external_id", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("close_time", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("platform", "external_id", name="uq_markets_platform_external_id"),
)

alert_rules = Table(
    "alert_rules", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("market_id", BigInteger, ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False),
    Column("threshold", Numeric(asdecimal=True), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("is_alerted", Boolean, nullable=False, server_default=text("false")),
    Column("last_alerted_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("threshold > 0 AND threshold < 1", name="ck_alert_rules_threshold"),
)

ingestion_runs = Table(
    "ingestion_runs", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("collection_slot", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False, server_default=text("'running'")),
    Column("attempted_count", Integer, nullable=False, server_default=text("0")),
    Column("inserted_count", Integer, nullable=False, server_default=text("0")),
    Column("duplicate_count", Integer, nullable=False, server_default=text("0")),
    Column("failed_count", Integer, nullable=False, server_default=text("0")),
    Column("error_summary", Text, nullable=True),
    Column("watchlist_config", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("market_outcomes", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    CheckConstraint("status IN ('running', 'success', 'partial', 'failed', 'incomplete')", name="ck_runs_status"),
    CheckConstraint("attempted_count >= 0 AND inserted_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0", name="ck_runs_nonnegative_counts"),
    CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="ck_runs_time_order"),
    CheckConstraint("jsonb_typeof(watchlist_config) = 'object' AND jsonb_typeof(market_outcomes) = 'object'", name="ck_runs_json_objects"),
)

snapshots = Table(
    "snapshots", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("market_id", BigInteger, ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False),
    Column("ingestion_run_id", BigInteger, ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=False),
    Column("collection_slot", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("source_timestamp", DateTime(timezone=True), nullable=True),
    # Unscaled NUMERIC avoids rounding before the precision checks run.
    Column("yes_bid", Numeric(asdecimal=True), nullable=True),
    Column("yes_bid_quantity", Numeric(asdecimal=True), nullable=True),
    Column("yes_ask", Numeric(asdecimal=True), nullable=True),
    Column("yes_ask_quantity", Numeric(asdecimal=True), nullable=True),
    UniqueConstraint("market_id", "collection_slot", name="uq_snapshots_market_slot"),
    CheckConstraint("yes_bid IS NULL OR (yes_bid BETWEEN 0 AND 1 AND scale(yes_bid) <= 4)", name="ck_snapshots_bid_price"),
    CheckConstraint("yes_ask IS NULL OR (yes_ask BETWEEN 0 AND 1 AND scale(yes_ask) <= 4)", name="ck_snapshots_ask_price"),
    CheckConstraint("yes_bid_quantity IS NULL OR (yes_bid_quantity >= 0 AND scale(yes_bid_quantity) <= 2 AND yes_bid_quantity NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))", name="ck_snapshots_bid_quantity"),
    CheckConstraint("yes_ask_quantity IS NULL OR (yes_ask_quantity >= 0 AND scale(yes_ask_quantity) <= 2 AND yes_ask_quantity NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))", name="ck_snapshots_ask_quantity"),
    CheckConstraint("(yes_bid IS NULL) = (yes_bid_quantity IS NULL)", name="ck_snapshots_bid_pair"),
    CheckConstraint("(yes_ask IS NULL) = (yes_ask_quantity IS NULL)", name="ck_snapshots_ask_pair"),
    Index("ix_snapshots_market_observed_id", "market_id", "observed_at", "id"),
)

"""Read stored metadata, latest snapshots, and bounded history; no exchange requests or writes."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection

from models import markets, snapshots


def list_markets(connection: Connection, limit: int, offset: int) -> list[dict]:
    statement = select(markets).order_by(markets.c.id).limit(limit).offset(offset)
    rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]


def find_market(connection: Connection, market_id: int) -> dict | None:
    statement = select(markets).where(markets.c.id == market_id)
    row = connection.execute(statement).mappings().one_or_none()
    return dict(row) if row is not None else None


def find_latest_snapshot(connection: Connection, market_id: int) -> dict | None:
    statement = select(snapshots).where(snapshots.c.market_id == market_id).order_by(
        snapshots.c.observed_at.desc(), snapshots.c.id.desc(),
    ).limit(1)
    row = connection.execute(statement).mappings().one_or_none()
    return dict(row) if row is not None else None


def market_has_observations(connection: Connection, market_id: int) -> bool:
    statement = select(snapshots.c.id).where(snapshots.c.market_id == market_id).limit(1)
    return connection.execute(statement).first() is not None


def list_history(connection: Connection, market_id: int, start: datetime, end: datetime, limit: int, offset: int) -> list[dict]:
    statement = select(snapshots).where(
        snapshots.c.market_id == market_id,
        snapshots.c.observed_at >= start,
        snapshots.c.observed_at < end,
    ).order_by(snapshots.c.observed_at, snapshots.c.id).limit(limit).offset(offset)
    rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]

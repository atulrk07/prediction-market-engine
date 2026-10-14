"""Step 3A: read stored market metadata; no exchange requests or writes."""

from sqlalchemy import select
from sqlalchemy.engine import Connection

from models import markets


def list_markets(connection: Connection, limit: int, offset: int) -> list[dict]:
    statement = select(markets).order_by(markets.c.id).limit(limit).offset(offset)
    rows = connection.execute(statement).mappings().all()
    return [dict(row) for row in rows]


def find_market(connection: Connection, market_id: int) -> dict | None:
    statement = select(markets).where(markets.c.id == market_id)
    row = connection.execute(statement).mappings().one_or_none()
    return dict(row) if row is not None else None

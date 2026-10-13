"""Small PostgreSQL connection configuration shared with Alembic."""

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


# Local development credentials, matching compose.yaml; override via environment.
DEFAULT_DATABASE_URL = "postgresql+psycopg://prediction_market:local_dev_password@127.0.0.1:5433/prediction_market"


def database_url(url: str | None = None) -> str:
    if url is None:
        url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use postgresql+psycopg")
    return url


def make_engine(url: str | None = None):
    return create_engine(
        database_url(url),
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=10000 -c lock_timeout=5000 -c timezone=UTC",
        },
        pool_timeout=5,
        pool_pre_ping=True,
    )

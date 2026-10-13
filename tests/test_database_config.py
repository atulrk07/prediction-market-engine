"""Small configuration checks; no database connection is opened."""

import pytest

from database import DEFAULT_DATABASE_URL, database_url


def test_default_targets_the_local_compose_service(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_url() == DEFAULT_DATABASE_URL


def test_environment_can_target_an_isolated_postgresql_database(monkeypatch):
    url = "postgresql+psycopg://localhost/prediction_market_test"
    monkeypatch.setenv("DATABASE_URL", url)
    assert database_url() == url


def test_sqlite_is_not_a_postgresql_substitute():
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        database_url("sqlite://")

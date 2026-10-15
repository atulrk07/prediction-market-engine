"""Make unintended real HTTP calls fail immediately in the offline suite."""

import httpx
import pytest
import os
import urllib.request
from sqlalchemy.engine import make_url

from database import make_engine


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch):
    def blocked_request(self, request):
        raise AssertionError("Offline tests must use httpx.MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked_request)

    def blocked_discord(*args, **kwargs):
        raise AssertionError("Offline tests must mock Discord HTTP")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", blocked_discord)
    # An inherited developer webhook must never be used by the tests.
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)


@pytest.fixture(scope="module")
def postgres_engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL checks")
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("Use an isolated database whose name ends in _test")
    engine = make_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()

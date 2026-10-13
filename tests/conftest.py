"""Make unintended real HTTP calls fail immediately in the offline suite."""

import httpx
import pytest


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch):
    def blocked_request(self, request):
        raise AssertionError("Offline tests must use httpx.MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked_request)

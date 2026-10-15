"""Bound GET attempts and retry waiting separately from total cycle elapsed time."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import random
import re
import time

import httpx


MAX_ATTEMPTS = 3
RETRY_STATUSES = {429, 500, 502, 503, 504}


class BudgetExhausted(Exception):
    pass


class CollectionStopped(Exception):
    pass


def retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        try:
            return float(value)
        except OverflowError:
            return math.inf
    try:
        at = parsedate_to_datetime(value)
        if at.tzinfo is None:
            return None
        return max(0.0, (at - now).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


class CycleBudget:
    def __init__(self, seconds: float, retry_seconds: float, *, monotonic=time.monotonic):
        if not math.isfinite(seconds) or seconds <= 0 or not math.isfinite(retry_seconds) or retry_seconds < 0:
            raise ValueError("Cycle budget must be positive; retry-wait budget must be nonnegative and finite")
        self.monotonic = monotonic
        self.started = monotonic()
        self.seconds = seconds
        self.retry_seconds = retry_seconds
        self.waited = 0.0
        self.blocked_reason = None

    def remaining(self) -> float:
        return max(0.0, self.seconds - (self.monotonic() - self.started))

    def check(self, stop):
        if stop.is_set():
            raise CollectionStopped("shutdown")
        if self.blocked_reason:
            raise BudgetExhausted(self.blocked_reason)
        if self.remaining() <= 0:
            raise BudgetExhausted("cycle_budget_exhausted")

    def wait_for_retry(self, delay: float, stop, *, instructed: bool):
        self.check(stop)
        cycle_too_short = delay >= self.remaining()
        if cycle_too_short or delay > max(0.0, self.retry_seconds - self.waited):
            if instructed:
                # Do not issue another request early after a server-directed delay.
                self.blocked_reason = "retry_after_exceeds_budget"
            raise BudgetExhausted(self.blocked_reason or ("cycle_budget_exhausted" if cycle_too_short else "retry_wait_budget_exhausted"))
        began = self.monotonic()
        interrupted = stop.wait(delay)
        self.waited += max(0.0, self.monotonic() - began)
        if interrupted or stop.is_set():
            raise CollectionStopped("shutdown_during_retry")
        self.check(stop)


class RetryingClient:
    """Supply get() to the existing fetch functions; reuse their validation unchanged."""

    def __init__(self, client, budget: CycleBudget, stop, run_id: int, ticker: str,
                 *, now=lambda: datetime.now(timezone.utc), uniform=random.uniform):
        self.client, self.budget, self.stop = client, budget, stop
        self.run_id, self.ticker = run_id, ticker
        self.now, self.uniform = now, uniform
        self.attempts = {"metadata": 0, "book": 0}
        self.stage = "metadata"

    def get(self, path, *, params=None):
        self.stage = "book" if path.endswith("/orderbook") else "metadata"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.budget.check(self.stop)
            remaining = self.budget.remaining()
            if remaining <= 0:
                raise BudgetExhausted("cycle_budget_exhausted")
            self.attempts[self.stage] += 1
            timeout = httpx.Timeout(min(10.0, remaining), connect=min(5.0, remaining))
            logging.info("run=%s ticker=%s stage=%s attempt=%s/%s", self.run_id, self.ticker, self.stage, attempt, MAX_ATTEMPTS)
            instructed = False
            try:
                response = self.client.get(path, params=params, timeout=timeout)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == MAX_ATTEMPTS:
                    raise
                delay = self.uniform(0, 0.5 * 2 ** (attempt - 1))
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                delay = retry_after_seconds(response.headers.get("Retry-After"), self.now())
                if attempt == MAX_ATTEMPTS:
                    if delay is not None and delay > 0:
                        self.budget.blocked_reason = "retry_after_after_final_attempt"
                    return response
                instructed = delay is not None
                if delay is None:
                    delay = self.uniform(0, 0.5 * 2 ** (attempt - 1))
                response.close()
            logging.info("run=%s ticker=%s retry_wait=%.3fs", self.run_id, self.ticker, delay)
            self.budget.wait_for_retry(delay, self.stop, instructed=instructed)

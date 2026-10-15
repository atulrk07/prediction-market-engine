"""A clock and stop flag that advance without sleeping; synthetic test use only."""

from datetime import datetime, timedelta, timezone


class FakeTime:
    def __init__(self):
        self.elapsed = 0.0
        self.waits = []
        self.stopped = False
        self.stop_during_wait = False

    def monotonic(self):
        return self.elapsed

    def now(self):
        return datetime(2026, 10, 14, 12, tzinfo=timezone.utc) + timedelta(seconds=self.elapsed)

    def advance(self, seconds):
        self.elapsed += seconds

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.stop_during_wait:
            self.advance(min(seconds, 0.1))
            self.stopped = True
            return True
        self.advance(seconds)
        return False

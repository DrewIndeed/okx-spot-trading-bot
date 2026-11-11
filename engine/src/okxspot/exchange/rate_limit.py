from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    """Thread-safe token bucket. ``acquire`` blocks until one token is available."""

    def __init__(
        self,
        rate_per_sec: float,
        *,
        capacity: float | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = rate_per_sec
        self.capacity = rate_per_sec if capacity is None else capacity
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._tokens = self.capacity
        self._updated = self._clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated)
                self._updated = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            self._sleeper(wait)


def backoff_seconds(attempt: int, *, base: float = 0.25, cap: float = 8.0) -> float:
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    if base <= 0 or cap <= 0:
        raise ValueError("base and cap must be positive")
    return float(min(cap, base * (2 ** (attempt - 1))))

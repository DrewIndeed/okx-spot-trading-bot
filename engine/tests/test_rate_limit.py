from __future__ import annotations

from okxspot.exchange.rate_limit import TokenBucket, backoff_seconds


def test_backoff_schedule() -> None:
    assert backoff_seconds(1) == 0.25
    assert backoff_seconds(2) == 0.5
    assert backoff_seconds(3) == 1.0


def test_token_bucket_waits_when_empty() -> None:
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    bucket = TokenBucket(10, capacity=1, clock=now, sleeper=sleep)
    bucket.acquire()
    bucket.acquire()
    assert clock["t"] == 0.1

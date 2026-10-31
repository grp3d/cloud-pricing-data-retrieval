"""
Request pacing for pricing sources (FR-032, research R2/R5).

A minimum interval between request starts (1 / rate) plus a cap on requests in flight.
`penalize()` pushes the next start back, for a throttling response's Retry-After.
Used as a context manager around each request.
"""

import threading
import time
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        rate_per_second: float,
        max_concurrent: int = 1,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate_per_second <= 0:
            raise ValueError(f"rate_per_second must be > 0, got {rate_per_second}")
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        self.interval = 1.0 / rate_per_second
        self._now = now
        self._sleep = sleep
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()
        self._next_start = None  # earliest time the next request may start

    def _wait_turn(self) -> None:
        with self._lock:
            now = self._now()
            start = now if self._next_start is None else max(now, self._next_start)
            self._next_start = start + self.interval
        if start > now:
            self._sleep(start - now)

    def penalize(self, seconds: float) -> None:
        """Delay the next request start by at least `seconds` from now."""
        with self._lock:
            until = self._now() + max(0.0, seconds)
            self._next_start = until if self._next_start is None else max(self._next_start, until)

    def __enter__(self) -> "RateLimiter":
        self._slots.acquire()
        try:
            self._wait_turn()
        except BaseException:
            self._slots.release()
            raise
        return self

    def __exit__(self, *exc) -> bool:
        self._slots.release()
        return False

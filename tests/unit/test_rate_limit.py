"""
Request pacing for pricing sources (FR-032, research R2/R5; feature 004 T007).
"""

import threading
import time

import pytest

from src.pipeline.rate_limit import RateLimiter


class FakeClock:
    """`sleep` advances `now`, so pacing is checked without real waiting."""

    def __init__(self):
        self.t = 1000.0
        self.lock = threading.Lock()

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.t += max(0.0, seconds)


def test_spaces_acquisitions_by_the_rate():
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=0.5, max_concurrent=1, now=clock.now, sleep=clock.sleep)
    starts = []
    for _ in range(5):
        with limiter:
            starts.append(clock.now())
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 2.0 - 1e-9 for g in gaps), gaps


def test_first_acquisition_does_not_wait():
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=1.0, max_concurrent=1, now=clock.now, sleep=clock.sleep)
    with limiter:
        assert clock.now() == 1000.0


def test_penalize_delays_the_next_acquisition():
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=10.0, max_concurrent=1, now=clock.now, sleep=clock.sleep)
    with limiter:
        pass
    limiter.penalize(30)
    before = clock.now()
    with limiter:
        assert clock.now() - before >= 30 - 1e-9


def test_concurrency_cap():
    limiter = RateLimiter(rate_per_second=1000.0, max_concurrent=2)
    holders, peak, guard = [0], [0], threading.Lock()

    def work():
        with limiter:
            with guard:
                holders[0] += 1
                peak[0] = max(peak[0], holders[0])
            time.sleep(0.01)
            with guard:
                holders[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] <= 2


@pytest.mark.parametrize("rate,conc", [(0, 1), (-1, 1), (1, 0)])
def test_rejects_invalid_settings(rate, conc):
    with pytest.raises(ValueError):
        RateLimiter(rate_per_second=rate, max_concurrent=conc)

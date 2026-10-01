"""
Retry policy for pricing file downloads (FR-047, research R9).

Delay before retry n (n >= 1):
    fixed               base
    linear_backoff      base * n
    exponential_backoff base * 2^(n-1)
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Tuple, TypeVar

T = TypeVar("T")


class RetriesExhausted(Exception):
    """The operation failed permanently or ran out of retries."""

    def __init__(self, last_error: BaseException, attempts: int):
        super().__init__(f"{last_error} (after {attempts} attempt(s))")
        self.last_error = last_error
        self.attempts = attempts


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    strategy: str = "exponential_backoff"
    base_delay_seconds: float = 30

    @classmethod
    def from_settings(cls, settings) -> "RetryPolicy":
        return cls(
            max_retries=settings.pricing_download_retry_max_retries,
            strategy=settings.pricing_download_retry_strategy,
            base_delay_seconds=settings.pricing_download_retry_base_delay_seconds,
        )

    def delay_for(self, retry_number: int) -> float:
        if self.strategy == "fixed":
            return self.base_delay_seconds
        if self.strategy == "linear_backoff":
            return self.base_delay_seconds * retry_number
        if self.strategy == "exponential_backoff":
            return self.base_delay_seconds * 2 ** (retry_number - 1)
        raise ValueError(f"Unknown retry strategy {self.strategy!r}")


def run_with_retry(
    fn: Callable[[], T],
    is_retryable: Callable[[BaseException], bool],
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[T, int]:
    """Call fn, retrying retryable errors per policy. Returns (result, attempts)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(), attempt
        except Exception as e:
            if not is_retryable(e) or attempt > policy.max_retries:
                raise RetriesExhausted(e, attempt) from e
            sleep(policy.delay_for(attempt))


async def run_with_retry_async(
    fn: Callable[[], Awaitable[T]],
    is_retryable: Callable[[BaseException], bool],
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Tuple[T, int]:
    """Async counterpart of run_with_retry."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn(), attempt
        except Exception as e:
            if not is_retryable(e) or attempt > policy.max_retries:
                raise RetriesExhausted(e, attempt) from e
            await sleep(policy.delay_for(attempt))

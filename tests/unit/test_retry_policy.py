"""RetryPolicy strategies and retry loop (FR-047, research R9)."""

import pytest

from src.pipeline.config import PipelineSettings
from src.pipeline.retry import RetriesExhausted, RetryPolicy, run_with_retry, run_with_retry_async


@pytest.mark.parametrize(
    "strategy,expected",
    [
        ("fixed", [30, 30, 30]),
        ("linear_backoff", [30, 60, 90]),
        ("exponential_backoff", [30, 60, 120]),
    ],
)
def test_delay_formulas(strategy, expected):
    policy = RetryPolicy(max_retries=3, strategy=strategy, base_delay_seconds=30)
    assert [policy.delay_for(n) for n in (1, 2, 3)] == expected


def test_from_settings_uses_pricing_download_retry_settings():
    s = PipelineSettings.from_env(
        {
            "PRICING_DOWNLOAD_RETRY_MAX_RETRIES": "2",
            "PRICING_DOWNLOAD_RETRY_STRATEGY": "fixed",
            "PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS": "5",
        }
    )
    policy = RetryPolicy.from_settings(s)
    assert (policy.max_retries, policy.strategy, policy.base_delay_seconds) == (2, "fixed", 5)


class Transient(Exception):
    pass


class Permanent(Exception):
    pass


def _flaky(fail_n, exc=Transient):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= fail_n:
            raise exc(f"fail {calls['n']}")
        return "ok"

    return fn, calls


def _is_retryable(e):
    return isinstance(e, Transient)


def test_retries_then_succeeds_and_reports_attempts(no_sleep):
    fn, calls = _flaky(2)
    policy = RetryPolicy(3, "exponential_backoff", 10)
    result, attempts = run_with_retry(fn, _is_retryable, policy, sleep=no_sleep)
    assert (result, attempts) == ("ok", 3)
    assert no_sleep.calls == [10, 20]


def test_zero_retries_never_retries(no_sleep):
    fn, calls = _flaky(1)
    with pytest.raises(RetriesExhausted) as info:
        run_with_retry(fn, _is_retryable, RetryPolicy(0, "fixed", 10), sleep=no_sleep)
    assert calls["n"] == 1
    assert info.value.attempts == 1
    assert no_sleep.calls == []


def test_permanent_error_is_not_retried(no_sleep):
    fn, calls = _flaky(5, exc=Permanent)
    with pytest.raises(RetriesExhausted) as info:
        run_with_retry(fn, _is_retryable, RetryPolicy(3, "fixed", 1), sleep=no_sleep)
    assert calls["n"] == 1
    assert isinstance(info.value.last_error, Permanent)
    assert info.value.attempts == 1


def test_exhausted_reraises_last_error_with_attempts(no_sleep):
    fn, calls = _flaky(10)
    with pytest.raises(RetriesExhausted) as info:
        run_with_retry(fn, _is_retryable, RetryPolicy(3, "linear_backoff", 2), sleep=no_sleep)
    assert calls["n"] == 4
    assert info.value.attempts == 4
    assert str(info.value.last_error) == "fail 4"
    assert no_sleep.calls == [2, 4, 6]


async def test_async_variant(no_sleep):
    state = {"n": 0}

    async def fn():
        state["n"] += 1
        if state["n"] == 1:
            raise Transient("once")
        return "ok"

    async def fake_sleep(seconds):
        no_sleep(seconds)

    result, attempts = await run_with_retry_async(
        fn, _is_retryable, RetryPolicy(3, "fixed", 7), sleep=fake_sleep
    )
    assert (result, attempts) == ("ok", 2)
    assert no_sleep.calls == [7]

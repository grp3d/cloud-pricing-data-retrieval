"""
Paging base class for HTTP pricing sources (FR-022, FR-032, FR-034; feature 004 T009).

A minimal subclass defined here stands in for a provider: pages link to the next page
through a `next` field in the body.
"""

import json

import pytest

from src.pipeline.http_pages import PagedHttpSource, PermanentSourceError, SourceError
from src.pipeline.rate_limit import RateLimiter
from src.pipeline.retry import RetryPolicy
from tests.helpers.fake_pricing import FakeHttpSource, http_error, http_timeout

BASE = "https://source.test/items"


def _page(n, last=False):
    return json.dumps({"items": [f"item-{n}"], "next": None if last else f"{BASE}?page={n + 1}"}).encode()


class LinkedSource(PagedHttpSource):
    secret = "SECRET-123"

    def first_url(self):
        return f"{BASE}?page=1"

    def next_url(self, data, url):
        return data.get("next")

    def request_headers(self):
        return {"X-Token": self.secret}

    def redact(self, text):
        return text.replace(self.secret, "***")


class RestartingSource(LinkedSource):
    def resume_rejected(self, status, body):
        return status == 400 and b"expired" in body


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def _fake(pages=3):
    fake = FakeHttpSource()
    for n in range(1, pages + 1):
        fake.route(f"{BASE}?page={n}", _page(n, last=n == pages))
    return fake


def _source(fake, cls=LinkedSource, retries=3, limiter=None, sleep=None):
    return cls(
        fetch=fake,
        limiter=limiter,
        retry_policy=RetryPolicy(max_retries=retries, strategy="fixed", base_delay_seconds=5),
        sleep=sleep or Sleeps(),
    )


def test_yields_every_page_in_order():
    fake = _fake(3)
    src = _source(fake)
    pages = list(src.pages())
    assert [p.number for p in pages] == [1, 2, 3]
    assert [p.data["items"] for p in pages] == [["item-1"], ["item-2"], ["item-3"]]
    assert src.stats.requests_made == 3
    assert src.stats.pages == 3


def test_failed_page_is_retried_from_its_own_url_not_page_one():
    fake = _fake(3).fail(f"{BASE}?page=2", http_timeout(1), http_error(503, times=1))
    src = _source(fake)
    list(src.pages())
    assert fake.urls() == [
        f"{BASE}?page=1",
        f"{BASE}?page=2",
        f"{BASE}?page=2",
        f"{BASE}?page=2",
        f"{BASE}?page=3",
    ]
    assert src.stats.requests_made == 5


def test_truncated_page_is_retried():
    fake = _fake(2)
    fake.fail(f"{BASE}?page=2", http_error(200, body=b'{"items": [', times=1))
    src = _source(fake)
    assert [p.number for p in src.pages()] == [1, 2]
    assert src.stats.requests_made == 3


def test_429_waits_retry_after_through_the_limiter():
    fake = _fake(1).fail(f"{BASE}?page=1", http_error(429, headers={"Retry-After": "7"}, times=1))

    class Clock:
        t = 0.0

    def now():
        return Clock.t

    def sleep(s):
        Clock.t += s

    limiter = RateLimiter(rate_per_second=100, now=now, sleep=sleep)
    src = _source(fake, limiter=limiter)
    list(src.pages())
    assert src.stats.throttled_responses == 1
    assert Clock.t >= 7


def test_subclass_retry_after_override_is_honored():
    class OtherHeader(LinkedSource):
        def retry_after_seconds(self, headers):
            return float(headers.get("x-wait", 0)) or super().retry_after_seconds(headers)

    sleeps = Sleeps()
    fake = _fake(1).fail(f"{BASE}?page=1", http_error(429, headers={"x-wait": "11"}, times=1))
    src = _source(fake, cls=OtherHeader, sleep=sleeps)
    list(src.pages())
    assert 11 in sleeps.calls


def test_retries_follow_the_policy_then_fail():
    sleeps = Sleeps()
    fake = _fake(2).fail(f"{BASE}?page=2", http_error(503))
    src = _source(fake, retries=2, sleep=sleeps)
    with pytest.raises(SourceError) as err:
        list(src.pages())
    assert "503" in str(err.value)
    assert src.stats.requests_made == 1 + 3  # page 1, then page 2 three times (1 + 2 retries)
    assert sleeps.calls == [5, 5]


def test_permanent_error_is_not_retried():
    fake = _fake(1).fail(f"{BASE}?page=1", http_error(403, body=b'{"reason": "API_KEY_INVALID"}'))
    src = _source(fake)
    with pytest.raises(PermanentSourceError) as err:
        list(src.pages())
    assert err.value.status == 403
    assert "API_KEY_INVALID" in str(err.value)
    assert src.stats.requests_made == 1


def test_resume_rejected_restarts_the_listing_once():
    fake = _fake(3).fail(f"{BASE}?page=3", http_error(400, body=b"expired token", times=1))
    src = _source(fake, cls=RestartingSource)
    numbers = [p.number for p in src.pages()]
    assert numbers == [1, 2, 1, 2, 3]
    assert src.stats.restarts == 1


def test_second_resume_rejection_fails():
    fake = _fake(3).fail(f"{BASE}?page=3", http_error(400, body=b"expired token", times=2))
    src = _source(fake, cls=RestartingSource)
    with pytest.raises(SourceError) as err:
        list(src.pages())
    assert not isinstance(err.value, PermanentSourceError)
    assert "rejected" in str(err.value)


def test_base_never_treats_a_rejection_as_resumable():
    fake = _fake(2).fail(f"{BASE}?page=2", http_error(400, body=b"expired token"))
    src = _source(fake)
    with pytest.raises(PermanentSourceError):
        list(src.pages())


def test_headers_are_sent_but_never_appear_in_errors():
    fake = _fake(1).fail(f"{BASE}?page=1", http_error(403, body=b"bad key SECRET-123"))
    src = _source(fake)
    with pytest.raises(PermanentSourceError) as err:
        list(src.pages())
    assert fake.requests[0][1] == {"X-Token": "SECRET-123"}
    assert "SECRET-123" not in str(err.value)
    assert "SECRET-123" not in repr(err.value)
    assert "SECRET-123" not in repr(src)


def test_error_messages_are_clipped():
    fake = _fake(1).fail(f"{BASE}?page=1", http_error(400, body=b"x" * 5000))
    src = _source(fake)
    with pytest.raises(PermanentSourceError) as err:
        list(src.pages())
    assert len(str(err.value)) <= 500


def test_explicit_first_url():
    fake = _fake(2)
    src = _source(fake)
    assert [p.number for p in src.pages(f"{BASE}?page=2")] == [1]

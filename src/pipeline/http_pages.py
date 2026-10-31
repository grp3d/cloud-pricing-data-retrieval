"""
Paging over an HTTP pricing source (FR-022, FR-032, FR-034; research R11).

`PagedHttpSource` holds what every paged source shares: the page loop, pacing through a
`RateLimiter`, retries that resume at the failed page (never page 1), throttling handling,
detection of a page that links back to an earlier one, request counting and error
redaction. Providers extend it in `src/<provider>/http_pages.py`
and override only what differs: how to find the next page, which headers to send, which
errors are permanent, whether the source rejected a resume point, and what to redact.

The transport is an injectable `fetch(url, headers, timeout) -> (status, headers, body)`,
so tests run offline.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Set, Tuple

from src.pipeline.rate_limit import RateLimiter
from src.pipeline.retry import RetryPolicy

MAX_MESSAGE = 500

Fetch = Callable[[str, Dict[str, str], float], Tuple[int, Dict[str, str], bytes]]


class SourceError(Exception):
    """The source could not be read, after retries where retrying could help."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message[:MAX_MESSAGE])
        self.status = status


class PermanentSourceError(SourceError):
    """An error retrying can't fix (e.g. a rejected credential). Never retried."""


@dataclass
class PageFetchStats:
    requests_made: int = 0
    throttled_responses: int = 0
    restarts: int = 0
    pages: int = 0


@dataclass
class Page:
    number: int  # 1-based within the listing; restarts at 1 if the listing restarts
    url: str
    body: bytes
    data: Any


class _Retryable(Exception):
    def __init__(self, message: str, status: Optional[int] = None, wait: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.wait = wait


class _ResumeRejected(Exception):
    pass


def urllib_fetch(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b""


class PagedHttpSource:
    def __init__(
        self,
        fetch: Optional[Fetch] = None,
        limiter: Optional[RateLimiter] = None,
        retry_policy: Optional[RetryPolicy] = None,
        timeout_seconds: float = 120,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._fetch = fetch or urllib_fetch
        self._limiter = limiter
        self._policy = retry_policy or RetryPolicy()
        self._timeout = timeout_seconds
        self._sleep = sleep
        self.stats = PageFetchStats()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(stats={self.stats})"

    # --- overridable ----------------------------------------------------------------

    def first_url(self) -> str:
        raise NotImplementedError

    def next_url(self, data: Any, url: str) -> Optional[str]:
        """The next page's URL, or None after the last page."""
        raise NotImplementedError

    def request_headers(self) -> Dict[str, str]:
        return {}

    def is_permanent_error(self, status: int, body: bytes) -> bool:
        return 400 <= status < 500 and status != 429

    def resume_rejected(self, status: int, body: bytes) -> bool:
        """True when the source refused a page link or token (e.g. expired), so the listing
        must start again from its first page (FR-034)."""
        return False

    def retry_after_seconds(self, headers: Dict[str, str]) -> Optional[float]:
        for name, value in headers.items():
            if name.lower() == "retry-after":
                try:
                    return max(0.0, float(value))
                except ValueError:
                    return None
        return None

    def redact(self, text: str) -> str:
        return text

    # --- shared ---------------------------------------------------------------------

    def _message(self, text: str) -> str:
        return self.redact(text)[:MAX_MESSAGE]

    def _get_once(self, url: str, page_number: int) -> Page:
        def call():
            return self._fetch(url, self.request_headers(), self._timeout)

        self.stats.requests_made += 1
        try:
            if self._limiter is not None:
                with self._limiter:
                    status, headers, body = call()
            else:
                status, headers, body = call()
        except (TimeoutError, OSError, urllib.error.URLError) as e:
            raise _Retryable(f"{type(e).__name__}: {e}")

        if status == 429:
            self.stats.throttled_responses += 1
            raise _Retryable("HTTP 429 throttled", status, self.retry_after_seconds(headers))
        if status >= 500:
            raise _Retryable(f"HTTP {status}", status)
        if status >= 400:
            if page_number > 1 and self.resume_rejected(status, body):
                raise _ResumeRejected(self._message(f"HTTP {status}: {body[:300].decode(errors='replace')}"))
            if self.is_permanent_error(status, body):
                raise PermanentSourceError(
                    self._message(f"HTTP {status} from {url}: {body[:400].decode(errors='replace')}"), status
                )
            raise _Retryable(f"HTTP {status}", status)
        try:
            data = json.loads(body)
        except ValueError:
            raise _Retryable("truncated or invalid page")
        return Page(page_number, url, body, data)

    def _get(self, url: str, page_number: int) -> Page:
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._get_once(url, page_number)
            except _Retryable as e:
                if attempt > self._policy.max_retries:
                    raise SourceError(
                        self._message(f"{e} for {url} after {attempt} attempt(s)"), e.status
                    ) from None
                wait = e.wait if e.wait is not None else self._policy.delay_for(attempt)
                if e.status == 429 and self._limiter is not None:
                    self._limiter.penalize(wait)
                else:
                    self._sleep(wait)

    def _get_linked(self, url: str, page_number: int, seen: Set[str]) -> Tuple[Page, Optional[str]]:
        """A page and its next URL. A page whose next link points back to a page already
        read in this listing is broken (it would loop forever), so it is retried like any
        other broken page and fails once the retries are used up."""
        attempt = 0
        while True:
            attempt += 1
            page = self._get(url, page_number)
            following = self.next_url(page.data, url)
            if following is None or following not in seen:
                return page, following
            if attempt > self._policy.max_retries:
                raise SourceError(
                    self._message(f"page {page_number} of {url} links back to a page already read, "
                                  f"after {attempt} attempt(s)")
                )
            self._sleep(self._policy.delay_for(attempt))

    def pages(self, first_url: Optional[str] = None) -> Iterator[Page]:
        """Every page of one listing, in order. If the source rejects a resume point, the
        listing restarts once from page 1 (consumers keyed by page number overwrite)."""
        start = first_url or self.first_url()
        restarted = False
        url, number = start, 1
        seen = {start}
        while url:
            try:
                page, following = self._get_linked(url, number, seen)
            except _ResumeRejected as e:
                if restarted:
                    raise SourceError(f"resume point rejected twice; {e}") from None
                restarted = True
                self.stats.restarts += 1
                url, number = start, 1
                seen = {start}
                continue
            self.stats.pages += 1
            yield page
            url = following
            if url:
                seen.add(url)
            number += 1

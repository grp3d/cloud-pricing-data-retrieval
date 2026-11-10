"""
Cloud Billing Catalog paging (research R4, R5): the key goes only in the `X-goog-api-key`
header and is scrubbed from every error message; pages follow `nextPageToken` (an empty
token ends the listing); an expired page token restarts the listing from page 1.
"""

from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.pipeline.http_pages import PagedHttpSource


class GcpPagedSource(PagedHttpSource):
    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        self._api_key = api_key

    def next_url(self, data: Any, url: str) -> Optional[str]:
        token = data.get("nextPageToken")
        if not token:
            return None
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "pageToken"]
        query.append(("pageToken", token))
        return urlunsplit(parts._replace(query=urlencode(query)))

    def request_headers(self) -> Dict[str, str]:
        return {"X-goog-api-key": self._api_key}

    def resume_rejected(self, status: int, body: bytes) -> bool:
        text = body.lower()
        return status == 400 and b"invalid_argument" in text and b"page token" in text

    def redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text

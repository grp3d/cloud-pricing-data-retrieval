"""
Azure Retail Prices paging (research R1, R2): pages link to the next one through
`NextPageLink`, and throttling responses may carry the wait in
`x-ms-ratelimit-retailPrices-retry-after` instead of `Retry-After`.
"""

from typing import Any, Dict, Optional

from src.pipeline.http_pages import PagedHttpSource

AZURE_RETRY_AFTER = "x-ms-ratelimit-retailprices-retry-after"


class AzurePagedSource(PagedHttpSource):
    def next_url(self, data: Any, url: str) -> Optional[str]:
        return data.get("NextPageLink") or None

    def retry_after_seconds(self, headers: Dict[str, str]) -> Optional[float]:
        standard = super().retry_after_seconds(headers)
        if standard is not None:
            return standard
        for name, value in headers.items():
            if name.lower() == AZURE_RETRY_AFTER:
                try:
                    return max(0.0, float(value))
                except ValueError:
                    return None
        return None

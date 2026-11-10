"""
Cloud Billing Catalog client (research R4, R5; FR-003, FR-031, FR-034).

The catalog is one download unit: the v1 services list (for each service's business
entity), then the catalog-wide SKU and price listings of `GCP_BILLING_API_VERSION`
(v2beta), each at the maximum page size. Pages are stored as received.
"""

import os
import time
from typing import Callable, Dict, Optional

from src.gcp.http_pages import GcpPagedSource
from src.pipeline.downloader import CatalogDownload
from src.pipeline.http_pages import SourceError
from src.pipeline.rate_limit import RateLimiter
from src.pipeline.retry import RetryPolicy

LISTINGS = ("services", "skus", "prices")


def listing_urls(settings) -> Dict[str, str]:
    gcp = settings.provider_settings
    base, version, size = gcp.billing_api_url, gcp.billing_api_version, gcp.page_size
    return {
        "services": f"{base}/v1/services?pageSize={size}",
        "skus": f"{base}/{version}/skus?pageSize={size}",
        "prices": f"{base}/{version}/skus/-/prices?pageSize={size}&currencyCode={settings.pricing_currency}",
    }


class GcpCatalogClient:
    def __init__(
        self,
        settings,
        fetch=None,
        limiter: Optional[RateLimiter] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self._source = GcpPagedSource(
            settings.provider_settings.api_key,
            fetch=fetch,
            limiter=limiter
            or RateLimiter(
                settings.pricing_download_rate_limit_per_second or 1.0,
                settings.pricing_download_max_concurrent_requests,
                sleep=sleep,
            ),
            retry_policy=RetryPolicy.from_settings(settings),
            sleep=sleep,
        )

    def download_catalog(self, dest_dir: str) -> CatalogDownload:
        os.makedirs(dest_dir, exist_ok=True)
        out = CatalogDownload(api_version=self.settings.provider_settings.billing_api_version, attempts=1)
        urls = listing_urls(self.settings)
        try:
            for name in LISTINGS:
                out.files += self._download_listing(urls[name], dest_dir, name)
        except SourceError as e:
            out.error = str(e)
        stats = self._source.stats
        out.counters = {"requests_made": stats.requests_made, "throttled_responses": stats.throttled_responses}
        return out

    def _download_listing(self, url: str, dest_dir: str, stem: str):
        last = 0
        for page in self._source.pages(url):
            with open(os.path.join(dest_dir, f"{stem}-p{page.number:04d}.json"), "wb") as fh:
                fh.write(page.body)
            last = page.number
        # A restarted listing overwrote its pages by number; drop any left from the first attempt.
        for name in os.listdir(dest_dir):
            if name.startswith(f"{stem}-p") and int(name[len(stem) + 2: len(stem) + 6]) > last:
                os.remove(os.path.join(dest_dir, name))
        return [os.path.join(dest_dir, f"{stem}-p{n:04d}.json") for n in range(1, last + 1)]

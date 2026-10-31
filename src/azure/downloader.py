"""
Azure downloader for the pipeline runner: one location's Retail Prices pages per call.
Locations run concurrently in the runner; they share one rate limiter (FR-032).
"""

import time
from typing import Callable, List, Optional

from src.azure.api import DEVTEST_PRICE_TYPE, AzurePricesClient
from src.pipeline.downloader import Downloader, RegionDownload


class AzurePricesDownloader(Downloader):
    def __init__(self, settings, log: Optional[Callable[[str], None]] = None, fetch=None, sleep=time.sleep):
        self.settings = settings
        self._log = log
        self._client = AzurePricesClient(settings, fetch=fetch, sleep=sleep)

    def download_region(self, region: str, dest_dir: str) -> RegionDownload:
        out = self._client.download_location(region, dest_dir)
        if self._log:
            self._log(f"azure {region}: {len(out.files)} page(s), {out.requests_made} request(s)")
        return RegionDownload(
            region=region,
            files=out.files,
            error=out.error,
            max_attempts=1 if out.requests_made else 0,
            counters={"requests_made": out.requests_made, "throttled_responses": out.throttled_responses},
        )

    def excluded_price_types(self) -> List[str]:
        return [] if self.settings.provider_settings.include_devtest_prices else [DEVTEST_PRICE_TYPE]

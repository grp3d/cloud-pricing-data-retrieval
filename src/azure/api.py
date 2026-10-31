"""
Azure Retail Prices client (research R1–R3; FR-002, FR-032, FR-034, FR-035).

One download unit per location: `armRegionName eq '<source name>'`, every page stored as
received. `global` makes two listings, `''` (no location) and `'Global'`. Partition names
map back to the source's spelling (`north-america` → `North America`).
"""

import os
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.parse import quote

from src.azure.http_pages import AzurePagedSource
from src.pipeline.http_pages import SourceError
from src.pipeline.rate_limit import RateLimiter
from src.pipeline.retry import RetryPolicy

GLOBAL = "global"
DEVTEST_PRICE_TYPE = "DevTestConsumption"

# Geographies used by data transfer prices (research R3), by partition name.
GEOGRAPHIES = {
    "north-america": "North America",
    "europe": "Europe",
    "asia": "Asia",
    "india": "India",
    "intercontinental": "Intercontinental",
    "middle-east-and-africa": "Middle East And Africa",
    "oceania": "Oceania",
    "south-america": "South America",
}


def filter_values(location: str) -> List[str]:
    """The `armRegionName` values to request for a partition name."""
    if location == GLOBAL:
        return ["", "Global"]
    return [GEOGRAPHIES.get(location, location)]


def listing_url(settings, location: str, source_value: Optional[str] = None) -> str:
    azure = settings.provider_settings
    value = filter_values(location)[0] if source_value is None else source_value
    expr = f"armRegionName eq '{value}'"
    if not azure.include_devtest_prices:
        expr += f" and type ne '{DEVTEST_PRICE_TYPE}'"
    query = f"api-version={quote(azure.prices_api_version)}"
    if settings.pricing_currency != "USD":
        query += f"&currencyCode={quote(settings.pricing_currency)}"
    return f"{azure.prices_api_url}?{query}&$filter={quote(expr)}"


@dataclass
class LocationDownload:
    location: str
    files: List[str] = field(default_factory=list)
    requests_made: int = 0
    throttled_responses: int = 0
    error: Optional[str] = None


class AzurePricesClient:
    def __init__(
        self,
        settings,
        fetch=None,
        limiter: Optional[RateLimiter] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self._fetch = fetch
        self._sleep = sleep
        self._policy = RetryPolicy.from_settings(settings)
        self._limiter = limiter or RateLimiter(
            settings.pricing_download_rate_limit_per_second or 0.5,
            settings.pricing_download_max_concurrent_requests,
            sleep=sleep,
        )

    def _source(self) -> AzurePagedSource:
        return AzurePagedSource(fetch=self._fetch, limiter=self._limiter, retry_policy=self._policy, sleep=self._sleep)

    def download_location(self, location: str, dest_dir: str) -> LocationDownload:
        out = LocationDownload(location)
        os.makedirs(dest_dir, exist_ok=True)
        for value in filter_values(location):
            stem = "prices" if location != GLOBAL else ("prices-empty" if value == "" else "prices-global")
            source = self._source()
            try:
                written = self._download_listing(source, listing_url(self.settings, location, value), dest_dir, stem)
            except SourceError as e:
                out.error = str(e)
            finally:
                out.requests_made += source.stats.requests_made
                out.throttled_responses += source.stats.throttled_responses
            if out.error:
                return out
            if location != GLOBAL and written == (0, 1):
                out.error = f"location {location!r} is unknown to the source (no prices returned)"
                return out
            out.files += [os.path.join(dest_dir, f"{stem}-p{n:04d}.json") for n in range(1, written[1] + 1)]
        return out

    @staticmethod
    def _download_listing(source: AzurePagedSource, url: str, dest_dir: str, stem: str):
        """Write every page as received; returns (items, pages). A restarted listing overwrites
        its earlier pages by number."""
        items, last = 0, 0
        for page in source.pages(url):
            if page.number == 1:
                items = 0
            with open(os.path.join(dest_dir, f"{stem}-p{page.number:04d}.json"), "wb") as fh:
                fh.write(page.body)
            items += len(page.data.get("Items") or [])
            last = page.number
        for stale in os.listdir(dest_dir):
            if stale.startswith(f"{stem}-p") and int(stale[len(stem) + 2: len(stem) + 6] or 0) > last:
                os.remove(os.path.join(dest_dir, stale))
        return items, last

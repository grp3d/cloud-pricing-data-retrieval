"""
Downloader base classes the runner uses to fetch raw pricing files.

- `Downloader`: one location at a time (`per_location` providers).
- `CatalogDownloader`: one catalog-wide download shared by every location (`catalog`).

Each provider package extends one of them in `src/<provider>/downloader.py`, and tests
inject fakes that extend them too (tests/helpers/fake_pricing.py), so runs work offline.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class FailedDownload:
    service: str
    reason: str
    attempts: int


@dataclass
class RegionDownload:
    """Outcome of downloading one region's raw pricing files into a local directory."""

    region: str
    files: List[str] = field(default_factory=list)
    failed_downloads: List[FailedDownload] = field(default_factory=list)
    services_without_price_list: int = 0
    max_attempts: int = 0
    # Set when the whole region failed before any per-file work (e.g. invalid region).
    error: Optional[str] = None
    # Optional manifest counters for this location: requests_made, throttled_responses.
    counters: Dict[str, int] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.error is None and not self.failed_downloads and bool(self.files)

    def failure_reason(self) -> str:
        if self.error:
            return self.error
        if self.failed_downloads:
            first = self.failed_downloads[0]
            more = len(self.failed_downloads) - 1
            suffix = f" (+{more} more)" if more else ""
            return f"{first.service}: {first.reason} after {first.attempts} attempt(s){suffix}"
        if not self.files:
            return "no pricing files downloaded"
        return ""


@dataclass
class CatalogDownload:
    """Outcome of a catalog-wide download into a local directory."""

    files: List[str] = field(default_factory=list)
    api_version: str = ""
    error: Optional[str] = None
    attempts: int = 0
    # requests_made, throttled_responses (manifest `catalog` block)
    counters: Dict[str, int] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.error is None and bool(self.files)


class Downloader(ABC):
    @abstractmethod
    def download_region(self, region: str, dest_dir: str) -> RegionDownload: ...

    def excluded_price_types(self) -> List[str]:
        """Source price types deliberately not requested (manifest `excluded_price_types`)."""
        return []


class CatalogDownloader(ABC):
    @abstractmethod
    def download_catalog(self, dest_dir: str) -> CatalogDownload: ...

    def excluded_price_types(self) -> List[str]:
        return []

"""
Downloader interface the runner uses to fetch one region's raw pricing files.

The runner only depends on `Downloader`; `AwsPricingDownloader` is the production
implementation (a thin wrapper over src.aws_pricing_api.run_pricing_job), and tests
inject a fake (tests/helpers/fake_pricing.py) so runs work offline.
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol


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


class Downloader(Protocol):
    def download_region(self, region: str, dest_dir: str) -> RegionDownload: ...


class AwsPricingDownloader:
    """Downloads every service's price list for a region via the AWS Price List API."""

    def __init__(self, settings, log: Optional[Callable[[str], None]] = None):
        self._settings = settings
        self._log = log

    def download_region(self, region: str, dest_dir: str) -> RegionDownload:
        from src.aws_pricing_api import PricingJobRequest, run_pricing_job
        from src.pipeline.retry import RetryPolicy

        request = PricingJobRequest(
            region=region,
            output_dir=dest_dir,
            output_format="json",
            all_services=True,
            max_raw_download_workers=self._settings.max_raw_download_workers,
            retry_policy=RetryPolicy.from_settings(self._settings),
            log_callback=self._log,
        )
        try:
            result = run_pricing_job(request)
        except Exception as e:  # credentials, invalid region, API errors
            return RegionDownload(region=region, error=f"{type(e).__name__}: {e}"[:500])

        outcome = RegionDownload(
            region=region,
            files=list(result.downloaded_files),
            failed_downloads=[
                FailedDownload(f["service"], f["reason"], f["attempts"])
                for f in result.failed_downloads
            ],
            services_without_price_list=result.services_without_price_list,
            max_attempts=result.max_attempts,
        )
        if not outcome.files and not outcome.failed_downloads:
            outcome.error = "; ".join(result.errors) or "no pricing files downloaded"
        return outcome

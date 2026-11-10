"""
AWS downloader for the pipeline runner: a thin wrapper over src.aws.api.run_pricing_job
that fetches every service's price list for one region.
"""

from typing import Callable, Optional

from src.pipeline.downloader import Downloader, FailedDownload, RegionDownload


class AwsPricingDownloader(Downloader):
    """Downloads every service's price list for a region via the AWS Price List API."""

    def __init__(self, settings, log: Optional[Callable[[str], None]] = None):
        self._settings = settings
        self._log = log

    def download_region(self, region: str, dest_dir: str) -> RegionDownload:
        from src.aws.api import PricingJobRequest, run_pricing_job
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

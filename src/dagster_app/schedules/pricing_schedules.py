from datetime import datetime
from typing import Iterator

from dagster import RunConfig, RunRequest, ScheduleEvaluationContext, schedule

from src.dagster_app.assets.pricing_assets import PricingDownloadConfig, TransformConfig
from src.dagster_app.jobs.pricing_jobs import pricing_pipeline_single_region
from src.dagster_app.resources import PricingRegionsResource


def _run_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _snapshot_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def build_region_run_requests(
    pricing_regions: PricingRegionsResource,
) -> Iterator[RunRequest]:
    """One RunRequest per configured region, against pricing_pipeline_single_region.

    Shared fan-out logic — the single source of truth for both
    pricing_weekly_schedule (cron-triggered) and
    trigger_configured_regions_sensor (manually/on-demand-triggered), so the
    two trigger mechanisms can never drift apart in behavior.
    """
    timestamp = _run_timestamp()
    snap_date = _snapshot_date()
    for region in pricing_regions.get_regions():
        yield RunRequest(
            run_key=f"{timestamp}-{region}",
            tags={"dagster/concurrency_key": "pricing-region-download"},
            run_config=RunConfig(
                ops={
                    "download_pricing": PricingDownloadConfig(
                        region=region,
                        raw_timestamp=timestamp,
                        output_format="json",
                        all_services=True,
                        max_raw_download_workers=2,
                    ),
                    "transform_to_parquet": TransformConfig(
                        region=region,
                        raw_timestamp=timestamp,
                        snapshot_date=snap_date,
                    ),
                }
            ),
        )


@schedule(job=pricing_pipeline_single_region, cron_schedule="0 13 * * 1")
def pricing_weekly_schedule(
    _context: ScheduleEvaluationContext, pricing_regions: PricingRegionsResource
) -> Iterator[RunRequest]:
    yield from build_region_run_requests(pricing_regions)

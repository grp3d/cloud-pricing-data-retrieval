from dagster import job

from src.dagster_app.assets.pricing_assets import download_pricing, transform_to_parquet


@job
def pricing_pipeline_single_region():
    """Single-region pricing download + transform.

    Every run of this job processes exactly one region, whatever region its
    PricingDownloadConfig says. It's the one job in this app: launch it
    manually from the Dagster UI Launchpad for ad hoc / backfill use, or let
    pricing_weekly_schedule / trigger_configured_regions_sensor launch N runs
    of it (one per configured region — see
    pricing_schedules.build_region_run_requests) for full region coverage.
    """
    raw_dir = download_pricing()
    transform_to_parquet(raw_dir)

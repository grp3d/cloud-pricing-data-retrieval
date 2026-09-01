from datetime import datetime

from dagster import RunConfig, RunRequest, ScheduleEvaluationContext, schedule

from src.dagster_app.assets.pricing_assets import PricingDownloadConfig, TransformConfig
from src.dagster_app.jobs.pricing_jobs import pricing_pipeline


def _run_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _snapshot_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


@schedule(job=pricing_pipeline, cron_schedule="0 13 * * *")
def pricing_weekly_schedule(_context: ScheduleEvaluationContext) -> RunRequest:
    timestamp = _run_timestamp()
    snap_date = _snapshot_date()
    return RunRequest(
        run_config=RunConfig(
            ops={
                "download_pricing": PricingDownloadConfig(
                    region="us-east-1",
                    raw_timestamp=timestamp,
                    output_format="json",
                    all_services=True,
                    max_raw_download_workers=2,
                ),
                "transform_to_parquet": TransformConfig(
                    region="us-east-1",
                    raw_timestamp=timestamp,
                    snapshot_date=snap_date,
                ),
            }
        )
    )

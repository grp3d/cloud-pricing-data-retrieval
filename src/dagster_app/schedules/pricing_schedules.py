"""Dagster schedules package (local runner only; the cloud uses EventBridge Scheduler)."""
from typing import Iterator

from dagster import RunConfig, RunRequest, ScheduleEvaluationContext, schedule

from src.dagster_app.assets.pricing_assets import SnapshotRunConfig
from src.dagster_app.jobs.pricing_jobs import pricing_snapshot
from src.dagster_app.resources import PricingRegionsResource


@schedule(job=pricing_snapshot, cron_schedule="0 13 * * 1")
def pricing_weekly_schedule(
    context: ScheduleEvaluationContext, pricing_regions: PricingRegionsResource
) -> Iterator[RunRequest]:
    """One run per week covering every configured region (a single snapshot)."""
    tick = context.scheduled_execution_time
    yield RunRequest(
        run_key=f"snapshot-{tick.strftime('%Y%m%dT%H%M%SZ')}",
        run_config=RunConfig(
            ops={
                "run_pricing_snapshot": SnapshotRunConfig(
                    regions=pricing_regions.get_regions(), trigger="scheduled"
                )
            }
        ),
    )

from typing import Iterator

from dagster import DefaultSensorStatus, RunRequest, SensorEvaluationContext, sensor

from src.dagster_app.jobs.pricing_jobs import pricing_pipeline_single_region
from src.dagster_app.resources import PricingRegionsResource
from src.dagster_app.schedules.pricing_schedules import build_region_run_requests


@sensor(
    job=pricing_pipeline_single_region,
    default_status=DefaultSensorStatus.STOPPED,
    minimum_interval_seconds=3600,
    description=(
        "Manual, on-demand trigger for the configured-regions pricing pipeline. "
        "Launches one run per configured region — the same fan-out as "
        "pricing_weekly_schedule — without waiting for the weekly cron tick or "
        "touching that schedule. In the Dagster UI, open this sensor and click "
        "'Test Sensor' to evaluate it immediately regardless of its running status."
    ),
)
def trigger_configured_regions_sensor(
    context: SensorEvaluationContext, pricing_regions: PricingRegionsResource
) -> Iterator[RunRequest]:
    yield from build_region_run_requests(pricing_regions)

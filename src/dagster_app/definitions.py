"""Dagster package for definitions, jobs and schedules (optional local runner, FR-007)."""
from dagster import Definitions

from src.dagster_app.jobs.pricing_jobs import pricing_snapshot
from src.dagster_app.resources import PricingRegionsResource
from src.dagster_app.schedules.pricing_schedules import pricing_weekly_schedule


defs = Definitions(
    jobs=[pricing_snapshot],
    schedules=[pricing_weekly_schedule],
    resources={"pricing_regions": PricingRegionsResource()},
)

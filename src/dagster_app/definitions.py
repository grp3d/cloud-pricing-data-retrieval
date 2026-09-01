from dagster import Definitions

from src.dagster_app.jobs.pricing_jobs import pricing_pipeline
from src.dagster_app.schedules.pricing_schedules import pricing_weekly_schedule


defs = Definitions(
    jobs=[pricing_pipeline],
    schedules=[pricing_weekly_schedule],
)

from dagster import Definitions

from src.dagster_app.jobs.pricing_jobs import pricing_pipeline_single_region
from src.dagster_app.resources import PricingRegionsResource
from src.dagster_app.schedules.pricing_schedules import pricing_weekly_schedule
from src.dagster_app.sensors.pricing_sensors import trigger_configured_regions_sensor


defs = Definitions(
    jobs=[pricing_pipeline_single_region],
    schedules=[pricing_weekly_schedule],
    sensors=[trigger_configured_regions_sensor],
    resources={"pricing_regions": PricingRegionsResource()},
)

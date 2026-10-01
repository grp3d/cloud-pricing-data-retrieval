"""Dagster jobs package."""
from dagster import job

from src.dagster_app.assets.pricing_assets import run_pricing_snapshot


@job
def pricing_snapshot():
    """Download, transform and publish one snapshot for all configured regions.

    Launch it from the Dagster UI Launchpad (optionally setting snapshot_date, regions or
    transform_only), or let pricing_weekly_schedule start it. It calls the same
    run_snapshot() entry point as the container (FR-007).
    """
    run_pricing_snapshot()

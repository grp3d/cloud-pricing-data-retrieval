"""
Dagster op wrapping the pipeline entry point (FR-007, research R16).

Dagster is an optional local runner only: the op builds PipelineSettings from the
environment and calls src.pipeline.runner.run_snapshot — the same code the container
runs in the cloud. One run covers the whole snapshot (all regions).
"""

from typing import List

from dagster import Config, OpExecutionContext, op

from src.dagster_app.resources import PricingRegionsResource
from src.pipeline.config import PipelineSettings
from src.pipeline.runner import RunRequest, run_snapshot


class SnapshotRunConfig(Config):
    snapshot_date: str = ""  # default: UTC date at start
    regions: List[str] = []  # default: the pricing_regions resource
    transform_only: bool = False
    trigger: str = "manual"


@op
def run_pricing_snapshot(
    context: OpExecutionContext,
    config: SnapshotRunConfig,
    pricing_regions: PricingRegionsResource,
) -> dict:
    settings = PipelineSettings.from_env()
    report = run_snapshot(
        settings,
        RunRequest(
            snapshot_date=config.snapshot_date or None,
            regions=list(config.regions) or pricing_regions.get_regions(),
            trigger=config.trigger,
            transform_only=config.transform_only,
        ),
        log=context.log.info,
    )
    context.log.info(report.to_json())
    if report.outcome != "completed":
        raise RuntimeError(f"Snapshot run {report.outcome}: {report.error or report.refused_by}")
    if report.snapshot_status == "failed":
        raise RuntimeError(f"Snapshot {report.snapshot_date} failed: {report.region_results}")
    if report.snapshot_status == "partial":
        context.log.warning(f"Snapshot {report.snapshot_date} is partial: {report.region_results}")
    return {"snapshot_date": report.snapshot_date, "status": report.snapshot_status, "run_id": report.run_id}

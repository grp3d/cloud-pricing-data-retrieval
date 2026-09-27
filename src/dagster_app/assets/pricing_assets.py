import os
from datetime import datetime
from typing import List

from dagster import Config, OpExecutionContext, op

from src.aws_pricing_api import (
    CredentialsError,
    PricingJobRequest,
    resolve_output_dir,
    run_pricing_job,
)
from src.dagster_app.resources import PricingRegionsResource
from src.pricing_parquet_transformations import (
    TransformRequest,
    transform_pricing_to_parquet,
)


def _run_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _snapshot_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _data_root() -> str:
    return resolve_output_dir("")


class PricingDownloadConfig(Config):
    region: str = "us-east-1"
    raw_timestamp: str = ""
    output_format: str = "json"
    all_services: bool = True
    max_raw_download_workers: int = 2
    service_codes: List[str] = []


class TransformConfig(Config):
    region: str = "us-east-1"
    raw_timestamp: str = ""
    snapshot_date: str = ""


@op
def download_pricing(context: OpExecutionContext, config: PricingDownloadConfig) -> str:
    timestamp = config.raw_timestamp or _run_timestamp()
    raw_dir = os.path.join(_data_root(), "pricing_aws", "raw", timestamp)
    os.makedirs(raw_dir, exist_ok=True)

    request = PricingJobRequest(
        region=config.region,
        output_dir=raw_dir,
        output_format=config.output_format,
        all_services=config.all_services,
        max_raw_download_workers=config.max_raw_download_workers,
        service_codes=config.service_codes,
        log_callback=context.log.info,
    )

    try:
        result = run_pricing_job(request)
    except CredentialsError as e:
        raise RuntimeError(f"AWS credential error: {e}") from e

    for err in result.errors:
        context.log.warning(f"Download warning: {err}")

    if result.failed_services:
        context.log.warning(
            f"Services with no data: {', '.join(result.failed_services)}"
        )

    if not result.downloaded_files:
        raise RuntimeError("No pricing files were downloaded.")

    context.log.info(
        f"Download complete. {len(result.downloaded_files)} file(s) saved to {raw_dir}"
    )
    return raw_dir


@op
def transform_to_parquet(
    context: OpExecutionContext,
    raw_dir: str,
    config: TransformConfig,
    pricing_regions: PricingRegionsResource,
) -> str:
    parquet_root = os.path.join(_data_root(), "pricing_aws", "parquet")
    snap_date = config.snapshot_date or _snapshot_date()

    # raw_dir may be shared across regions in a multi-region run (schedule
    # fans out with one raw_timestamp for all regions), so only pick up this
    # job's own region's downloads — not every region's files sitting in
    # the same directory.
    region_suffix = f"-{config.region}.json"
    json_files = [
        os.path.join(raw_dir, file_name)
        for file_name in os.listdir(raw_dir)
        if file_name.endswith(region_suffix)
    ]
    if not json_files:
        raise RuntimeError(f"No JSON files found in {raw_dir}")

    request = TransformRequest(
        json_files=json_files,
        parquet_root=parquet_root,
        region=config.region,
        snapshot_date=snap_date,
        log_callback=context.log.info,
        expected_regions=pricing_regions.get_regions(),
    )
    result = transform_pricing_to_parquet(request)

    for err in result.errors:
        context.log.warning(f"Transform warning: {err}")

    if result.tables_skipped:
        context.log.warning(f"Tables skipped (no rows): {', '.join(result.tables_skipped)}")

    if not result.success:
        raise RuntimeError("Parquet transformation produced no output tables.")

    for table_name, outcome in result.marker_outcomes.items():
        if outcome.startswith(("INCOMPLETE", "SKIPPED")):
            context.log.warning(f"Marker {table_name}: {outcome}")
        else:
            context.log.info(f"Marker {table_name}: {outcome}")

    if result.marker_errors:
        for err in result.marker_errors:
            context.log.error(err)
        raise RuntimeError(
            f"Failed to write partition markers: {'; '.join(result.marker_errors)}"
        )

    if result.parse_failed_files:
        raise RuntimeError(
            "Input files failed to parse; region not marked complete: "
            f"{result.parse_failed_files}"
        )

    context.log.info(
        f"Transformation complete. Tables written: {', '.join(result.tables_written)}"
    )
    return result.parquet_root

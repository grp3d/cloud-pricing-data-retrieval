from dagster import job

from src.dagster_app.assets.pricing_assets import download_pricing, transform_to_parquet


@job
def pricing_pipeline():
    raw_dir = download_pricing()
    transform_to_parquet(raw_dir)

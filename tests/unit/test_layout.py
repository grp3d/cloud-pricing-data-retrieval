"""Exact key strings from contracts/storage-layout.md (FR-010)."""

import datetime as dt
import re

import pytest

from src.pipeline import layout

RUN = "20261005T130004Z-a1b2c3"


def test_raw_keys():
    assert layout.raw_run_prefix("aws", "2026-10-05", "us-east-1", RUN) == (
        f"aws/raw/2026-10-05/us-east-1/{RUN}/"
    )
    assert layout.raw_file_key("aws", "2026-10-05", "us-east-1", RUN, "pricing-AmazonEC2-us-east-1.json") == (
        f"aws/raw/2026-10-05/us-east-1/{RUN}/pricing-AmazonEC2-us-east-1.json.zst"
    )
    assert layout.raw_prefix("aws") == "aws/raw/"


def test_parquet_keys():
    assert layout.parquet_file_key("aws", "price_fact", "2026-10-05", "eu-west-1", RUN) == (
        f"aws/parquet/price_fact/snapshot_date=2026-10-05/region=eu-west-1/part-{RUN}.parquet"
    )
    assert layout.parquet_file_key("aws", "price_fact", "2026-10-05", "eu-west-1", RUN, part=2) == (
        f"aws/parquet/price_fact/snapshot_date=2026-10-05/region=eu-west-1/part-{RUN}-2.parquet"
    )
    assert layout.parquet_date_prefix("aws", "service_dim", "2026-10-05") == (
        "aws/parquet/service_dim/snapshot_date=2026-10-05/"
    )


def test_manifest_and_claim_keys():
    assert layout.manifest_key("aws", "2026-10-05") == "aws/manifests/2026-10-05/manifest.json"
    assert layout.revision_key("aws", "2026-10-05", 3) == "aws/manifests/2026-10-05/revisions/0003.json"
    assert layout.manifest_date_prefix("aws", "2026-10-05") == "aws/manifests/2026-10-05/"
    assert layout.latest_key("aws") == "aws/manifests/latest.json"
    assert layout.claim_key("aws", "2026-10-05") == "aws/claims/2026-10-05.json"


def test_parse_revision_key():
    assert layout.parse_revision_key("aws/manifests/2026-10-05/revisions/0012.json") == 12
    assert layout.parse_revision_key("aws/manifests/2026-10-05/manifest.json") is None


def test_tables_come_from_the_provider():
    assert layout.tables_for("aws") == ("service_dim", "product_dim", "product_attribute", "region_dim", "price_fact")
    assert layout.tables_for("azure") == ("price_item", "price_item_savings_plan")
    assert layout.tables_for("gcp") == ("service", "sku", "sku_price", "sku_price_tier")


def test_new_run_id_format_and_sortable():
    t1 = dt.datetime(2026, 10, 5, 13, 0, 4, tzinfo=dt.timezone.utc)
    t2 = t1 + dt.timedelta(seconds=1)
    a, b = layout.new_run_id(t1), layout.new_run_id(t2)
    assert re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}", a)
    assert a.startswith("20261005T130004Z-")
    assert a < b


@pytest.mark.parametrize(
    "call",
    [
        lambda: layout.manifest_key("aws", "2026-10-5"),
        lambda: layout.manifest_key("aws", "../etc"),
        lambda: layout.manifest_key("AWS", "2026-10-05"),
        lambda: layout.parquet_file_key("aws", "price_fact", "2026-10-05", "../x", RUN),
        lambda: layout.parquet_file_key("aws", "bogus_table", "2026-10-05", "us-east-1", RUN),
        lambda: layout.raw_run_prefix("aws", "2026-10-05", "us-east-1", "not-a-run-id"),
    ],
)
def test_malformed_inputs_rejected(call):
    with pytest.raises(ValueError):
        call()


# --- feature 004: locations and per-provider tables (T013) ----------------------------


@pytest.mark.parametrize("location", ["eastus", "us-east-1", "us-east1", "north-america", "us", "eu", "global"])
def test_locations_accepted(location):
    assert layout.validate_region(location) == location


@pytest.mark.parametrize("location", ["East US", "-x", "a_b", "", "x-", "US"])
def test_locations_rejected(location):
    with pytest.raises(ValueError):
        layout.validate_region(location)


def test_new_provider_parquet_keys():
    assert layout.parquet_file_key("azure", "price_item", "2026-10-05", "eastus", RUN) == (
        f"azure/parquet/price_item/snapshot_date=2026-10-05/region=eastus/part-{RUN}.parquet"
    )
    assert layout.parquet_file_key("gcp", "sku_price_tier", "2026-10-05", "global", RUN) == (
        f"gcp/parquet/sku_price_tier/snapshot_date=2026-10-05/region=global/part-{RUN}.parquet"
    )


@pytest.mark.parametrize(
    "provider,table", [("azure", "price_fact"), ("aws", "sku"), ("gcp", "price_item"), ("nope", "sku")]
)
def test_table_must_belong_to_the_provider(provider, table):
    with pytest.raises(ValueError):
        layout.parquet_table_prefix(provider, table)


def test_catalog_raw_keys():
    assert layout.catalog_raw_prefix("gcp", "2026-10-05", RUN) == f"gcp/raw/2026-10-05/catalog/{RUN}/"
    assert layout.catalog_raw_file_key("gcp", "2026-10-05", RUN, "skus-p0001.json") == (
        f"gcp/raw/2026-10-05/catalog/{RUN}/skus-p0001.json.zst"
    )

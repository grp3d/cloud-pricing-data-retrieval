"""
Tests for the cross-region row filter in pricing_parquet_transformations.py.

AWS's per-region Price List Query API is not reliably scoped to the requested
region (observed heavily on AmazonEC2, where a file downloaded for region A
also contains rows genuinely labeled with region B's regionCode). Without a
filter, transform_pricing_to_parquet() would write those foreign-region rows
into region A's partition. product_dim/product_attribute must only contain
rows whose own regionCode matches the region the job targeted.
"""

import json
import os

import pandas as pd
import pytest

from src.pricing_parquet_transformations import TransformRequest, transform_pricing_to_parquet


def _write_json(path: str, products: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "offerCode": "AmazonEC2",
                "publicationDate": "2026-09-15T00:00:00Z",
                "products": products,
                "terms": {},
            },
            fh,
        )


def test_foreign_region_rows_are_dropped(tmp_path):
    json_file = tmp_path / "pricing-AmazonEC2-eu-west-1.json"
    _write_json(
        str(json_file),
        {
            "SKU-LOCAL": {
                "productFamily": "Compute Instance",
                "attributes": {
                    "servicecode": "AmazonEC2",
                    "servicename": "Amazon EC2",
                    "regionCode": "eu-west-1",
                    "location": "EU (Ireland)",
                    "locationType": "AWS Region",
                    "instanceType": "m5.large",
                },
            },
            "SKU-FOREIGN": {
                "productFamily": "Compute Instance",
                "attributes": {
                    "servicecode": "AmazonEC2",
                    "servicename": "Amazon EC2",
                    "regionCode": "us-east-1",
                    "location": "US East (N. Virginia)",
                    "locationType": "AWS Region",
                    "instanceType": "m5.large",
                },
            },
        },
    )

    parquet_root = tmp_path / "parquet"
    request = TransformRequest(
        json_files=[str(json_file)],
        parquet_root=str(parquet_root),
        region="eu-west-1",
        snapshot_date="2026-09-15",
    )
    result = transform_pricing_to_parquet(request)

    assert result.success is True
    product_dim = pd.read_parquet(
        os.path.join(
            parquet_root, "product_dim", "snapshot_date=2026-09-15", "region=eu-west-1"
        )
    )
    assert set(product_dim["sku"]) == {"SKU-LOCAL"}
    assert set(product_dim["region_code"]) == {"eu-west-1"}

    attributes = pd.read_parquet(
        os.path.join(
            parquet_root, "product_attribute", "snapshot_date=2026-09-15", "region=eu-west-1"
        )
    )
    assert set(attributes["sku"]) == {"SKU-LOCAL"}

    # region_dim is a reference catalog, not a per-row correctness table, so
    # it may still record that us-east-1 was observed in this file.
    region_dim = pd.read_parquet(
        os.path.join(
            parquet_root, "region_dim", "snapshot_date=2026-09-15", "region=eu-west-1"
        )
    )
    assert {"eu-west-1", "us-east-1"}.issubset(set(region_dim["region_code"]))


def test_rows_without_region_code_default_to_target_region(tmp_path):
    json_file = tmp_path / "pricing-AWSDataTransfer-eu-west-1.json"
    _write_json(
        str(json_file),
        {
            "SKU-TRANSFER": {
                "productFamily": "Data Transfer",
                "attributes": {
                    "servicecode": "AWSDataTransfer",
                    "servicename": "AWS Data Transfer",
                    "fromRegionCode": "us-west-2",
                    "toRegionCode": "sa-east-1",
                },
            },
        },
    )

    parquet_root = tmp_path / "parquet"
    request = TransformRequest(
        json_files=[str(json_file)],
        parquet_root=str(parquet_root),
        region="eu-west-1",
        snapshot_date="2026-09-15",
    )
    result = transform_pricing_to_parquet(request)

    assert result.success is True
    product_dim = pd.read_parquet(
        os.path.join(
            parquet_root, "product_dim", "snapshot_date=2026-09-15", "region=eu-west-1"
        )
    )
    assert set(product_dim["sku"]) == {"SKU-TRANSFER"}
    assert set(product_dim["region_code"]) == {"eu-west-1"}


def test_marker_outcomes_reported_for_every_table(tmp_path):
    json_file = tmp_path / "pricing-AmazonEC2-eu-west-1.json"
    _write_json(
        str(json_file),
        {
            "SKU-LOCAL": {
                "productFamily": "Compute Instance",
                "attributes": {
                    "servicecode": "AmazonEC2",
                    "servicename": "Amazon EC2",
                    "regionCode": "eu-west-1",
                    "location": "EU (Ireland)",
                    "locationType": "AWS Region",
                    "instanceType": "m5.large",
                },
            },
        },
    )

    request = TransformRequest(
        json_files=[str(json_file)],
        parquet_root=str(tmp_path / "parquet"),
        region="eu-west-1",
        snapshot_date="2026-09-15",
        expected_regions=["eu-west-1"],
    )
    result = transform_pricing_to_parquet(request)

    assert set(result.marker_outcomes) == {
        "service_dim",
        "region_dim",
        "product_dim",
        "product_attribute",
        "price_fact",
    }
    for table in ["service_dim", "region_dim", "product_dim", "product_attribute"]:
        assert result.marker_outcomes[table] == "WRITTEN"
    # _write_json writes no terms, so price_fact has no rows in its only region.
    assert result.marker_outcomes["price_fact"] == "NO_DATA"
    assert result.marker_errors == []


def _write_single_region_input(tmp_path, region="eu-west-1"):
    json_file = tmp_path / f"pricing-AmazonEC2-{region}.json"
    _write_json(
        str(json_file),
        {
            "SKU-LOCAL": {
                "productFamily": "Compute Instance",
                "attributes": {
                    "servicecode": "AmazonEC2",
                    "servicename": "Amazon EC2",
                    "regionCode": region,
                    "location": "EU (Ireland)",
                    "locationType": "AWS Region",
                    "instanceType": "m5.large",
                },
            },
        },
    )
    return json_file


def test_marker_failure_recorded(tmp_path, monkeypatch):
    json_file = _write_single_region_input(tmp_path)

    def fail(*args, **kwargs):
        raise PermissionError("read-only partition")

    monkeypatch.setattr("src.pricing_parquet_transformations.mark_region_complete", fail)
    result = transform_pricing_to_parquet(
        TransformRequest(
            json_files=[str(json_file)],
            parquet_root=str(tmp_path / "parquet"),
            region="eu-west-1",
            snapshot_date="2026-09-15",
            expected_regions=["eu-west-1"],
        )
    )

    assert result.success is True
    assert result.marker_errors
    assert all("read-only partition" in err for err in result.marker_errors)


def _run_transform_op(tmp_path, monkeypatch, raw_files):
    from dagster import build_op_context

    from src.dagster_app.assets.pricing_assets import TransformConfig, transform_to_parquet
    from src.dagster_app.resources import PricingRegionsResource

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    for name, content in raw_files.items():
        (raw_dir / name).write_text(content)
    monkeypatch.setattr(
        "src.dagster_app.assets.pricing_assets._data_root", lambda: str(tmp_path / "data")
    )
    context = build_op_context(
        resources={"pricing_regions": PricingRegionsResource(regions=["eu-west-1"])}
    )
    return transform_to_parquet(
        context,
        raw_dir=str(raw_dir),
        config=TransformConfig(region="eu-west-1", snapshot_date="2026-09-15"),
    )


def test_transform_op_raises_on_marker_errors(tmp_path, monkeypatch):
    good = _write_single_region_input(tmp_path).read_text()

    def fail(*args, **kwargs):
        raise PermissionError("read-only partition")

    monkeypatch.setattr("src.pricing_parquet_transformations.mark_region_complete", fail)
    with pytest.raises(RuntimeError, match="Failed to write partition markers"):
        _run_transform_op(
            tmp_path, monkeypatch, {"pricing-AmazonEC2-eu-west-1.json": good}
        )


def test_transform_op_raises_on_parse_errors(tmp_path, monkeypatch):
    good = _write_single_region_input(tmp_path).read_text()

    with pytest.raises(RuntimeError, match="failed to parse"):
        _run_transform_op(
            tmp_path,
            monkeypatch,
            {
                "pricing-AmazonEC2-eu-west-1.json": good,
                "pricing-AmazonS3-eu-west-1.json": "{not json",
            },
        )

    partition = tmp_path / "data" / "pricing_aws" / "parquet" / "product_dim"
    region_dir = partition / "snapshot_date=2026-09-15" / "region=eu-west-1"
    assert (region_dir / "part-0.parquet").exists()
    assert not (region_dir / "_REGION_COMPLETE").exists()


def test_transform_op_succeeds_and_marks_partition(tmp_path, monkeypatch):
    good = _write_single_region_input(tmp_path).read_text()

    _run_transform_op(tmp_path, monkeypatch, {"pricing-AmazonEC2-eu-west-1.json": good})

    marker = (
        tmp_path / "data" / "pricing_aws" / "parquet" / "product_dim"
        / "snapshot_date=2026-09-15" / "_SUCCESS"
    )
    assert marker.is_file()

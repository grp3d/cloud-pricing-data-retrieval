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

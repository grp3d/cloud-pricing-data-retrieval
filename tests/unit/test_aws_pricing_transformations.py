"""
aws_pricing_transformations: raw price-list JSON → per-region Parquet files in a staging
directory, with per-file stats for the manifest (feature 003, tasks T019; C1, C3).

Also keeps the cross-region row filter from feature 001: AWS's per-region price list is
not reliably scoped to the requested region, so product rows labeled with another
regionCode must not land in this region's partition.
"""

import hashlib
import json
import os

import pandas as pd
import pyarrow.parquet as pq

from src.aws_pricing_transformations import (
    TABLE_SCHEMA_VERSIONS,
    TransformRequest,
    transform_pricing_to_parquet,
)
from src.pipeline.layout import TABLES

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures", "pricing")
RUN = "20261005T130004Z-a1b2c3"
DATE = "2026-10-05"


def _fixture_files(region):
    return sorted(
        os.path.join(FIXTURES, name) for name in os.listdir(FIXTURES) if name.endswith(f"-{region}.json")
    )


def _run(tmp_path, region="us-east-1", files=None):
    return transform_pricing_to_parquet(
        TransformRequest(
            json_files=files or _fixture_files(region),
            staging_dir=str(tmp_path / "staging"),
            region=region,
            run_id=RUN,
            snapshot_date=DATE,
        )
    )


def _file(result, table):
    matches = [f for f in result.files if f.table == table]
    assert len(matches) <= 1
    return matches[0] if matches else None


def test_writes_run_specific_files_in_staging_layout(tmp_path):
    result = _run(tmp_path)
    assert result.success is True
    for table in TABLES:
        f = _file(result, table)
        expected = os.path.join(
            tmp_path, "staging", table, f"snapshot_date={DATE}", "region=us-east-1", f"part-{RUN}.parquet"
        )
        assert f.local_path == expected
        assert os.path.isfile(expected)


def test_file_stats_match_written_files(tmp_path):
    result = _run(tmp_path)
    for f in result.files:
        data = open(f.local_path, "rb").read()
        assert f.bytes == len(data)
        assert f.sha256 == hashlib.sha256(data).hexdigest()
        assert f.row_count == pq.ParquetFile(f.local_path).metadata.num_rows
        assert f.region == "us-east-1"
    assert result.row_counts == {f.table: f.row_count for f in result.files}


def test_foreign_region_products_are_dropped(tmp_path):
    result = _run(tmp_path)
    products = pd.read_parquet(_file(result, "product_dim").local_path)
    assert "EC2-FOREIGN" not in set(products["sku"])
    assert set(products["region_code"]) == {"us-east-1"}
    attributes = pd.read_parquet(_file(result, "product_attribute").local_path)
    assert "EC2-FOREIGN" not in set(attributes["sku"])


def test_unparseable_price_is_null_and_counted(tmp_path):
    result = _run(tmp_path)
    prices = pd.read_parquet(_file(result, "price_fact").local_path)
    bad = prices[prices["sku"] == "S3-USEAST1-REQ"]
    assert len(bad) == 1
    assert bad["price"].isna().all()  # never estimated or defaulted (constitution I)
    assert result.unparseable_prices == 1

    other = _run(tmp_path / "eu", region="eu-west-1")
    assert other.unparseable_prices == 0


def test_zero_row_table_produces_no_file(tmp_path):
    no_terms = tmp_path / "pricing-AmazonEC2-us-east-1.json"
    doc = json.load(open(_fixture_files("us-east-1")[0]))
    doc["terms"] = {}
    no_terms.write_text(json.dumps(doc))
    result = _run(tmp_path, files=[str(no_terms)])
    assert _file(result, "price_fact") is None
    assert result.row_counts["price_fact"] == 0
    assert "price_fact" in result.tables_skipped
    assert _file(result, "product_dim") is not None


def test_no_marker_or_lock_files_are_created(tmp_path):
    _run(tmp_path)
    names = {name for _, dirs, files in os.walk(tmp_path) for name in files + dirs}
    assert not names & {"_SUCCESS", "_REGION_COMPLETE", ".locks"}


def test_table_schema_versions_cover_all_tables():
    assert TABLE_SCHEMA_VERSIONS == {table: 1 for table in TABLES}


def test_parse_failure_is_reported_not_swallowed(tmp_path):
    broken = tmp_path / "pricing-AmazonS3-us-east-1.json"
    broken.write_text("{not json")
    result = _run(tmp_path, files=_fixture_files("us-east-1")[:1] + [str(broken)])
    assert result.parse_failed_files == [str(broken)]
    assert any("not json" in e or "Expecting" in e for e in result.errors)


def test_rows_without_region_code_default_to_target_region(tmp_path):
    transfer = tmp_path / "pricing-AWSDataTransfer-eu-west-1.json"
    transfer.write_text(
        json.dumps(
            {
                "offerCode": "AWSDataTransfer",
                "publicationDate": "2026-09-15T00:00:00Z",
                "products": {
                    "SKU-TRANSFER": {
                        "productFamily": "Data Transfer",
                        "attributes": {
                            "servicecode": "AWSDataTransfer",
                            "servicename": "AWS Data Transfer",
                            "fromRegionCode": "us-west-2",
                            "toRegionCode": "sa-east-1",
                        },
                    }
                },
                "terms": {},
            }
        )
    )
    result = _run(tmp_path, region="eu-west-1", files=[str(transfer)])
    products = pd.read_parquet(_file(result, "product_dim").local_path)
    assert set(products["region_code"]) == {"eu-west-1"}

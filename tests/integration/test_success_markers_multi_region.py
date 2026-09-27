"""
Integration tests for partition success markers across regions (FR-001–FR-017).

Each region's Dagster run calls transform_pricing_to_parquet() once for its own
region against a shared parquet_root and snapshot date; these tests do the same,
one call per region, and check the _SUCCESS / _REGION_COMPLETE lifecycle.
"""

import json
import os

import pyarrow.dataset as ds

from src.partition_markers import REGION_COMPLETE_MARKER, SUCCESS_MARKER
from src.pricing_parquet_transformations import TransformRequest, transform_pricing_to_parquet

SNAP = "2026-09-27"
REGIONS = ["us-east-1", "eu-west-1", "ap-northeast-1"]
TABLES = ["service_dim", "region_dim", "product_dim", "product_attribute", "price_fact"]


def _write_pricing_json(path: str, region: str, with_terms: bool = True) -> None:
    sku = f"SKU-{region}"
    terms = {}
    if with_terms:
        terms = {
            "OnDemand": {
                sku: {
                    f"{sku}.TERM": {
                        "termAttributes": {},
                        "priceDimensions": {
                            f"{sku}.TERM.DIM": {
                                "unit": "Hrs",
                                "pricePerUnit": {"USD": "0.096"},
                                "description": "m5.large",
                            }
                        },
                    }
                }
            }
        }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "offerCode": "AmazonEC2",
                "publicationDate": "2026-09-15T00:00:00Z",
                "products": {
                    sku: {
                        "productFamily": "Compute Instance",
                        "attributes": {
                            "servicecode": "AmazonEC2",
                            "servicename": "Amazon EC2",
                            "regionCode": region,
                            "location": region,
                            "locationType": "AWS Region",
                            "instanceType": "m5.large",
                        },
                    }
                },
                "terms": terms,
            },
            fh,
        )


def _run_region(
    tmp_path,
    region,
    expected_regions=REGIONS,
    snapshot_date=SNAP,
    with_terms=True,
    extra_files=(),
):
    raw_dir = tmp_path / "raw" / snapshot_date
    raw_dir.mkdir(parents=True, exist_ok=True)
    json_file = raw_dir / f"pricing-AmazonEC2-{region}.json"
    _write_pricing_json(str(json_file), region, with_terms=with_terms)
    request = TransformRequest(
        json_files=[str(json_file), *extra_files],
        parquet_root=str(tmp_path / "parquet"),
        region=region,
        snapshot_date=snapshot_date,
        expected_regions=list(expected_regions),
    )
    return transform_pricing_to_parquet(request)


def _success(tmp_path, table, snapshot_date=SNAP):
    return tmp_path / "parquet" / table / f"snapshot_date={snapshot_date}" / SUCCESS_MARKER


def _region_dir(tmp_path, table, region, snapshot_date=SNAP):
    return tmp_path / "parquet" / table / f"snapshot_date={snapshot_date}" / f"region={region}"


# --- User Story 1 -----------------------------------------------------------


def test_success_written_only_after_last_region(tmp_path):
    _run_region(tmp_path, "us-east-1")
    result = _run_region(tmp_path, "eu-west-1")

    for table in TABLES:
        assert not _success(tmp_path, table).exists(), table
    assert result.marker_outcomes["price_fact"] == "INCOMPLETE (missing: ap-northeast-1)"

    result = _run_region(tmp_path, "ap-northeast-1")

    for table in TABLES:
        assert _success(tmp_path, table).is_file(), table
        assert result.marker_outcomes[table] == "WRITTEN"


def test_region_with_zero_rows_for_table_counts_as_done(tmp_path):
    _run_region(tmp_path, "us-east-1")
    _run_region(tmp_path, "eu-west-1", with_terms=False)
    _run_region(tmp_path, "ap-northeast-1")

    empty_region = _region_dir(tmp_path, "price_fact", "eu-west-1")
    assert sorted(os.listdir(empty_region)) == [REGION_COMPLETE_MARKER]
    assert _success(tmp_path, "price_fact").is_file()


def test_all_regions_empty_table_gets_no_success(tmp_path):
    result = None
    for region in REGIONS:
        result = _run_region(tmp_path, region, with_terms=False)

    assert not _success(tmp_path, "price_fact").exists()
    assert result.marker_outcomes["price_fact"] == "NO_DATA"
    assert _success(tmp_path, "product_dim").is_file()


def test_markers_do_not_change_row_counts(tmp_path):
    for region in REGIONS:
        _run_region(tmp_path, region)

    for table in ["product_dim", "price_fact"]:
        dataset = ds.dataset(
            str(tmp_path / "parquet" / table / f"snapshot_date={SNAP}"), format="parquet"
        )
        assert dataset.count_rows() == len(REGIONS), table


def test_removed_region_folder_ignored(tmp_path):
    _run_region(tmp_path, "sa-east-1", expected_regions=["sa-east-1"] + REGIONS)
    stale = _region_dir(tmp_path, "price_fact", "sa-east-1")
    (stale / REGION_COMPLETE_MARKER).unlink()

    for region in REGIONS:
        _run_region(tmp_path, region)

    assert _success(tmp_path, "price_fact").is_file()


# --- User Story 2 -----------------------------------------------------------


def _tree_snapshot(root):
    snapshot = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            stat = os.stat(path)
            snapshot[os.path.relpath(path, root)] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def test_other_snapshot_dates_untouched(tmp_path):
    old_snap = "2026-09-20"
    for region in REGIONS:
        _run_region(tmp_path, region, snapshot_date=old_snap)
    for table in ["price_fact", "product_attribute"]:
        _success(tmp_path, table, old_snap).unlink()

    old_partitions = {
        table: tmp_path / "parquet" / table / f"snapshot_date={old_snap}" for table in TABLES
    }
    before = {table: _tree_snapshot(path) for table, path in old_partitions.items()}

    for region in REGIONS:
        _run_region(tmp_path, region)

    after = {table: _tree_snapshot(path) for table, path in old_partitions.items()}
    assert after == before
    assert not _success(tmp_path, "price_fact", old_snap).exists()


def test_marker_locations(tmp_path):
    for region in REGIONS:
        _run_region(tmp_path, region)
    parquet_root = tmp_path / "parquet"

    success_markers = list(parquet_root.rglob(SUCCESS_MARKER))
    region_markers = list(parquet_root.rglob(REGION_COMPLETE_MARKER))

    assert len(success_markers) == len(TABLES)
    assert all(p.parent.name.startswith("snapshot_date=") for p in success_markers)
    assert len(region_markers) == len(TABLES) * len(REGIONS)
    assert all(p.parent.name.startswith("region=") for p in region_markers)
    for table in TABLES:
        assert not (parquet_root / table / SUCCESS_MARKER).exists()


# --- User Story 3 -----------------------------------------------------------


def test_rerun_region_removes_then_restores_success(tmp_path, monkeypatch):
    for region in REGIONS:
        _run_region(tmp_path, region)
    assert _success(tmp_path, "price_fact").is_file()

    import src.pricing_parquet_transformations as transformations

    original_write = transformations._write_parquet_table
    checked = []

    def write_asserting_marker_removed(df, table_name, parquet_root, snapshot_date, region):
        assert not _success(tmp_path, table_name).exists(), table_name
        checked.append(table_name)
        return original_write(df, table_name, parquet_root, snapshot_date, region)

    monkeypatch.setattr(transformations, "_write_parquet_table", write_asserting_marker_removed)
    result = _run_region(tmp_path, "eu-west-1")

    assert sorted(checked) == sorted(TABLES)
    for table in TABLES:
        assert _success(tmp_path, table).is_file(), table
        assert result.marker_outcomes[table] == "WRITTEN"


def test_rerun_with_now_empty_table_removes_stale_data(tmp_path):
    for region in REGIONS:
        _run_region(tmp_path, region)

    _run_region(tmp_path, "eu-west-1", with_terms=False)

    region_dir = _region_dir(tmp_path, "price_fact", "eu-west-1")
    assert sorted(os.listdir(region_dir)) == [REGION_COMPLETE_MARKER]


def test_corrupt_input_file_blocks_region(tmp_path):
    bad_file = tmp_path / "pricing-AmazonS3-eu-west-1.json"
    bad_file.write_text("{not json")

    _run_region(tmp_path, "us-east-1")
    result = _run_region(tmp_path, "eu-west-1", extra_files=[str(bad_file)])
    _run_region(tmp_path, "ap-northeast-1")

    assert result.parse_failed_files == [str(bad_file)]
    for table in TABLES:
        assert result.marker_outcomes[table] == "SKIPPED (input parse errors)"
        assert not (_region_dir(tmp_path, table, "eu-west-1") / REGION_COMPLETE_MARKER).exists()
        assert not _success(tmp_path, table).exists(), table

    retry = _run_region(tmp_path, "eu-west-1")

    assert retry.parse_failed_files == []
    for table in TABLES:
        assert _success(tmp_path, table).is_file(), table


def test_failed_table_write_only_blocks_that_table(tmp_path, monkeypatch):
    _run_region(tmp_path, "us-east-1")
    _run_region(tmp_path, "eu-west-1")

    import src.pricing_parquet_transformations as transformations

    original_write = transformations._write_parquet_table

    def failing_write(df, table_name, parquet_root, snapshot_date, region):
        if table_name == "product_attribute":
            raise OSError("simulated disk failure")
        return original_write(df, table_name, parquet_root, snapshot_date, region)

    monkeypatch.setattr(transformations, "_write_parquet_table", failing_write)
    result = _run_region(tmp_path, "ap-northeast-1")

    assert result.marker_outcomes["product_attribute"] == "SKIPPED (write failed)"
    assert not _success(tmp_path, "product_attribute").exists()
    for table in ["service_dim", "region_dim", "product_dim", "price_fact"]:
        assert _success(tmp_path, table).is_file(), table

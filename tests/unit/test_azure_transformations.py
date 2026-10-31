"""
Azure pages → price_item, price_item_savings_plan (data-model.md § Azure tables;
FR-013, FR-014, FR-017; feature 004 T022). Test-first, against the recorded fixtures.
"""

import datetime as dt
import hashlib
import json
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.azure.transformations import AzureTransform, partition_name
from src.pipeline.config import PipelineSettings
from tests.helpers.fake_pricing import AZURE_FIXTURES

RUN = "20261030T130000Z-abcdef"
DATE = "2026-10-30"
SETTINGS = PipelineSettings.from_env({"PIPELINE_PROVIDER": "azure"})


def _fixture(name):
    return os.path.join(AZURE_FIXTURES, name)


def _items(*names):
    out = []
    for name in names:
        with open(_fixture(name)) as fh:
            out += json.load(fh)["Items"]
    return out


def _run(tmp_path, location, *names):
    transform = AzureTransform(SETTINGS, [location])
    result = transform.transform_location(
        transform.load([_fixture(n) for n in names]), location, str(tmp_path / "staging"), RUN, DATE
    )
    tables = {f.table: pq.read_table(f.local_path) for f in result.files}
    return result, tables


@pytest.fixture
def eastus(tmp_path):
    return _run(tmp_path, "eastus", "eastus-p0001.json", "eastus-p0002.json")


def test_both_tables_are_written_with_the_contract_schema(eastus):
    result, tables = eastus
    assert result.ok and not result.errors
    assert set(tables) == {"price_item", "price_item_savings_plan"}
    schema = tables["price_item"].schema
    assert schema.names == [
        "price_item_key", "snapshot_date", "location", "arm_region_name", "location_display_name",
        "service_family", "service_name", "service_id", "product_id", "product_name", "sku_id", "sku_name",
        "arm_sku_name", "meter_id", "meter_name", "price_type", "reservation_term", "unit_of_measure",
        "tier_minimum_units", "currency_code", "unit_price", "retail_price", "effective_start_date",
        "effective_end_date", "is_primary_meter_region", "extra_fields",
    ]
    assert schema.field("snapshot_date").type == pa.date32()
    assert schema.field("tier_minimum_units").type == pa.float64()
    assert schema.field("unit_price").type == pa.float64()
    assert schema.field("effective_start_date").type == pa.timestamp("ms", tz="UTC")
    assert schema.field("is_primary_meter_region").type == pa.bool_()
    assert tables["price_item_savings_plan"].schema.names == [
        "price_item_key", "snapshot_date", "location", "term", "unit_price", "retail_price",
    ]
    assert AzureTransform.TABLE_SCHEMA_VERSIONS == {"price_item": 1, "price_item_savings_plan": 1}


def test_counts_duplicates_and_unparseable(eastus):
    result, tables = eastus
    received = len(_items("eastus-p0001.json", "eastus-p0002.json"))
    assert result.counters["items_received"] == received == 18
    assert result.counters["duplicates_removed"] == 1
    assert result.counters["unlocated_rows"] == 0
    assert tables["price_item"].num_rows == received - 1  # SC-004: rows + duplicates = received
    assert result.unparseable_prices == 1


def test_values_are_kept_as_published(eastus):
    _, tables = eastus
    rows = tables["price_item"].to_pylist()
    bad = next(r for r in rows if r["meter_name"] == "D4s v5 Low Priority (bad price)")
    assert bad["unit_price"] is None and bad["retail_price"] is not None
    reservation = next(r for r in rows if r["price_type"] == "Reservation")
    assert reservation["reservation_term"] in ("1 Year", "3 Years")
    consumption = next(r for r in rows if r["price_type"] == "Consumption")
    assert consumption["reservation_term"] is None
    assert consumption["effective_start_date"].tzinfo is not None
    assert {r["location"] for r in rows} == {"eastus"}
    assert {r["snapshot_date"] for r in rows} == {dt.date(2026, 10, 30)}
    ended = [r for r in rows if r["effective_end_date"] is not None]
    assert len(ended) == 1
    extra = next(r for r in rows if r["meter_name"] == "D4s v5 (extra field)")
    assert json.loads(extra["extra_fields"]) == {"newSourceField": {"note": "hand-edited"}}
    assert all(r["extra_fields"] is None for r in rows if r["meter_name"] != "D4s v5 (extra field)")


def test_tiers_are_separate_rows(eastus):
    _, tables = eastus
    tiers = sorted(
        r["tier_minimum_units"] for r in tables["price_item"].to_pylist() if r["meter_name"] == "Hot RA-GZRS Data Stored"
    )
    assert tiers == [0.0, 51200.0, 512000.0]


def test_price_item_key_and_savings_plans(eastus):
    _, tables = eastus
    linux = next(i for i in _items("eastus-p0001.json") if i.get("savingsPlan"))
    canonical = json.dumps({k: v for k, v in linux.items() if k != "savingsPlan"}, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    plans = [r for r in tables["price_item_savings_plan"].to_pylist() if r["price_item_key"] == key]
    assert sorted(r["term"] for r in plans) == ["1 Year", "3 Years"]
    assert any(r["price_item_key"] == key for r in tables["price_item"].to_pylist())
    assert tables["price_item_savings_plan"].num_rows == 2


def test_key_is_stable_across_runs(tmp_path):
    a = _run(tmp_path / "a", "eastus", "eastus-p0001.json")[1]["price_item"].column("price_item_key").to_pylist()
    b = _run(tmp_path / "b", "eastus", "eastus-p0001.json")[1]["price_item"].column("price_item_key").to_pylist()
    assert a == b


def test_global_files_both_listings_and_counts_unlocated(tmp_path):
    result, tables = _run(tmp_path, "global", "global-empty-p0001.json", "global-global-p0001.json")
    assert result.ok
    rows = tables["price_item"].to_pylist()
    assert {r["location"] for r in rows} == {"global"}
    assert sorted({r["arm_region_name"] for r in rows}) == ["", "Global"]
    assert result.counters["unlocated_rows"] == sum(1 for r in rows if r["arm_region_name"] == "")
    assert result.counters["unlocated_rows"] == 4


def test_geography_location(tmp_path):
    result, tables = _run(tmp_path, "north-america", "north-america-p0001.json")
    assert result.ok
    rows = tables["price_item"].to_pylist()
    assert {(r["location"], r["arm_region_name"]) for r in rows} == {("north-america", "North America")}


@pytest.mark.parametrize(
    "source,expected",
    [("eastus", "eastus"), ("North America", "north-america"), ("Middle East  And Africa", "middle-east-and-africa"),
     ("", "global"), ("Global", "global")],
)
def test_partition_name(source, expected):
    assert partition_name(source) == expected


def test_colliding_source_names_fail_the_location(tmp_path):
    items = _items("north-america-p0001.json")
    clash = dict(items[0], armRegionName="north america", meterId="clash")
    path = tmp_path / "clash.json"
    path.write_text(json.dumps({"Items": items + [clash], "NextPageLink": None, "Count": len(items) + 1}))
    transform = AzureTransform(SETTINGS, ["north-america"])
    result = transform.transform_location([str(path)], "north-america", str(tmp_path / "s"), RUN, DATE)
    assert not result.ok
    assert "north america" in result.failure_reason() and "North America" in result.failure_reason()


def test_unreadable_page_fails_the_location(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"Items": [')
    transform = AzureTransform(SETTINGS, ["eastus"])
    result = transform.transform_location([str(path)], "eastus", str(tmp_path / "s"), RUN, DATE)
    assert not result.ok

"""
GCP catalog → service, sku, sku_price, sku_price_tier (data-model.md § GCP tables,
§ Location; FR-013, FR-015, FR-017, FR-031, SC-004; feature 004 T029). Test-first,
against the recorded fixtures.
"""

import datetime as dt
import glob
import json
import os
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.gcp.transformations import GcpTransform
from src.pipeline.config import PipelineSettings
from tests.helpers.fake_pricing import GCP_FIXTURES

RUN = "20261030T130000Z-abcdef"
DATE = "2026-10-30"
N2_CORE, N2_RAM = "BB77-5FDA-69D9", "5B01-D157-A097"
REGIONAL_US_EAST1 = "0002-C3CC-1713"
REGIONAL_EUROPE_WEST1 = "0009-6F35-3126"
REGIONAL_US_UPPER = "2D3F-8043-D389"
GLOBAL_SKU = "0003-CFD5-F4DB"
NO_GEO = "00CC-93E2-6DC7"
NO_TAXONOMY = "53CC-D5FE-D895"
MAPS_SKU = "0164-F76D-680A"
TIERED = "0002-17F9-0731"
UNSPECIFIED = "00DF-CF91-B364"
MILLION_UNITS = "001B-3860-A0CF"
NO_PRICE = "006A-EFE2-3E22"


def _settings():
    return PipelineSettings.from_env({"PIPELINE_PROVIDER": "gcp", "GCP_PRICING_API_KEY": "TEST-KEY"})


def _files():
    return sorted(glob.glob(os.path.join(GCP_FIXTURES, "*.json")))


def _skus():
    out = []
    for path in sorted(glob.glob(os.path.join(GCP_FIXTURES, "skus-*.json"))):
        with open(path) as fh:
            out += json.load(fh)["skus"]
    return out


class Built:
    def __init__(self, tmp_path, configured):
        self.transform = GcpTransform(_settings(), configured + ["global"])
        self.catalog = self.transform.load(_files())
        self.counters = self.transform.run_counters(self.catalog)
        self.results, self.tables = {}, {}
        for location in configured + ["global"]:
            result = self.transform.transform_location(self.catalog, location, str(tmp_path / location), RUN, DATE)
            self.results[location] = result
            self.tables[location] = {f.table: pq.read_table(f.local_path) for f in result.files}

    def rows(self, location, table):
        return self.tables[location][table].to_pylist() if table in self.tables[location] else []

    def sku_ids(self, location):
        return {r["sku_id"] for r in self.rows(location, "sku")}


@pytest.fixture
def built(tmp_path):
    return Built(tmp_path, ["us-east1", "us-central1"])


def test_schemas(built):
    tables = built.tables["global"]
    assert set(tables) == {"service", "sku", "sku_price", "sku_price_tier"}
    assert tables["service"].schema.names == ["snapshot_date", "service_id", "service_name", "display_name", "business_entity_name"]
    assert tables["sku"].schema.names == [
        "snapshot_date", "location", "sku_id", "sku_name", "service_id", "display_name", "product_taxonomy",
        "geo_taxonomy_type", "geo_regions", "extra_fields",
    ]
    assert tables["sku"].schema.field("product_taxonomy").type == pa.list_(pa.string())
    assert tables["sku_price"].schema.names == [
        "snapshot_date", "location", "sku_id", "consumption_model", "consumption_model_description", "value_type",
        "currency_code", "unit", "unit_description", "unit_quantity", "aggregation_level", "aggregation_interval",
        "extra_fields",
    ]
    assert tables["sku_price"].schema.field("unit_quantity").type == pa.decimal128(38, 9)
    assert tables["sku_price_tier"].schema.names == [
        "snapshot_date", "location", "sku_id", "consumption_model", "start_amount", "currency_code", "list_price",
    ]
    assert tables["sku_price_tier"].schema.field("list_price").type == pa.decimal128(38, 9)
    assert tables["sku_price_tier"].schema.field("start_amount").type == pa.decimal128(38, 9)
    assert GcpTransform.TABLE_SCHEMA_VERSIONS == {"service": 1, "sku": 1, "sku_price": 1, "sku_price_tier": 1}


def test_multi_regional_skus_are_filed_under_each_configured_member_region(built):
    for location in ("us-east1", "us-central1"):
        assert {N2_CORE, N2_RAM} <= built.sku_ids(location)
    core = [r for r in built.rows("us-east1", "sku") if r["sku_id"] == N2_CORE][0]
    assert core["geo_taxonomy_type"] == "TYPE_MULTI_REGIONAL"
    assert core["geo_regions"] == ["us-central1", "us-east1", "us-west1"]
    assert core["product_taxonomy"][:3] == ["GCP", "Compute", "GCE"]
    assert core["service_id"] == "6F81-5844-456A"
    assert core["sku_name"] == f"skus/{N2_CORE}"
    assert N2_CORE not in built.sku_ids("global")


def test_regional_global_and_unlocated_filing(built):
    assert REGIONAL_US_EAST1 in built.sku_ids("us-east1")
    assert REGIONAL_US_EAST1 not in built.sku_ids("us-central1")
    assert GLOBAL_SKU in built.sku_ids("global")
    assert NO_GEO in built.sku_ids("global")
    no_geo = [r for r in built.rows("global", "sku") if r["sku_id"] == NO_GEO][0]
    assert no_geo["geo_taxonomy_type"] is None and no_geo["geo_regions"] is None
    assert built.results["global"].counters["unlocated_rows"] == 1
    assert built.counters["skus_unlocated"] == 1


def test_unconfigured_and_out_of_scope_skus_are_counted_not_stored(built):
    everywhere = set().union(*(built.sku_ids(loc) for loc in built.tables))
    assert REGIONAL_EUROPE_WEST1 not in everywhere
    assert REGIONAL_US_UPPER not in everywhere
    assert MAPS_SKU not in everywhere
    assert built.counters["skus_out_of_scope"] == 1


def test_upper_case_region_files_under_its_lowercase_partition(tmp_path):
    b = Built(tmp_path, ["us"])
    row = [r for r in b.rows("us", "sku") if r["sku_id"] == REGIONAL_US_UPPER][0]
    assert row["location"] == "us" and row["geo_regions"] == ["US"]


def test_service_rows_only_in_global(built):
    services = built.rows("global", "service")
    assert {r["business_entity_name"] for r in services} == {"businessEntities/GCP"}
    assert "Compute Engine" in {r["display_name"] for r in services}
    assert all(r["service_id"] and r["service_name"] == f"services/{r['service_id']}" for r in services)
    assert built.rows("us-east1", "service") == []


def test_missing_taxonomy_is_null(built):
    row = [r for r in built.rows("global", "sku") if r["sku_id"] == NO_TAXONOMY][0]
    assert row["product_taxonomy"] is None


def test_prices_and_tiers(built):
    prices = [r for r in built.rows("us-east1", "sku_price") if r["sku_id"] == N2_CORE]
    assert sorted(r["consumption_model_description"] for r in prices) == [
        "Compute Flexible CUD - 1 Year", "Compute Flexible CUD - 3 Year", "Default",
    ]
    default = [r for r in prices if r["consumption_model_description"] == "Default"][0]
    assert not default["consumption_model"].startswith("consumptionModels/")
    assert default["value_type"] == "rate" and default["unit"] == "h"
    assert default["unit_quantity"] == Decimal("1.000000000")
    tiers = [
        r for r in built.rows("us-east1", "sku_price_tier")
        if r["sku_id"] == N2_CORE and r["consumption_model"] == default["consumption_model"]
    ]
    assert len(tiers) == 1
    assert tiers[0]["start_amount"] == Decimal("0E-9")
    assert tiers[0]["list_price"] == Decimal("0.031611000")
    # price rows repeat in every location the SKU is filed under
    assert len([r for r in built.rows("us-central1", "sku_price") if r["sku_id"] == N2_CORE]) == 3


def _all(built, table):
    return [r for loc in built.tables for r in built.rows(loc, table)]


def test_tiered_unspecified_and_unit_quantity(tmp_path):
    b = Built(tmp_path, ["northamerica-northeast1", "me-central2"])
    tiers = [r for r in _all(b, "sku_price_tier") if r["sku_id"] == TIERED]
    by_model = {}
    for r in tiers:
        by_model.setdefault(r["consumption_model"], []).append(r["start_amount"])
    assert max(len(v) for v in by_model.values()) == 3
    assert all(v == sorted(v) for v in by_model.values())
    million = [r for r in _all(b, "sku_price") if r["sku_id"] == MILLION_UNITS]
    assert million and million[0]["unit_quantity"] == Decimal("1000000")
    malformed = [r for r in _all(b, "sku_price_tier") if r["sku_id"] == MILLION_UNITS]
    assert any(r["list_price"] is None for r in malformed)
    assert b.results["me-central2"].unparseable_prices >= 1
    unspecified = [r for r in _all(b, "sku_price") if r["sku_id"] == UNSPECIFIED]
    assert unspecified
    for r in unspecified:
        assert r["value_type"] == "unspecified"
        assert r["unit"] is None and r["unit_quantity"] is None and r["aggregation_level"] is None


def test_malformed_list_price_is_null_and_counted(tmp_path):
    transform = GcpTransform(_settings(), ["global"])
    catalog = transform.load(_files())
    total = sum(r.unparseable_prices for r in [transform.transform_location(catalog, loc, str(tmp_path / loc), RUN, DATE)
                                             for loc in catalog.locations_with_skus()])
    assert total >= 1


def test_sku_without_price_and_price_without_sku(built):
    assert NO_PRICE in built.sku_ids("us-east1")
    assert not [r for r in built.rows("us-east1", "sku_price") if r["sku_id"] == NO_PRICE]
    assert built.counters["skus_without_price"] == 1
    assert built.counters["prices_without_sku"] == 1


def test_counters_account_for_every_sku(built):
    stored = set().union(*(built.sku_ids(loc) for loc in built.tables))
    c = built.counters
    assert c["skus_received"] == len({s["skuId"] for s in _skus()})
    assert c["skus_received"] == len(stored) + c["skus_out_of_scope"] + c["skus_unconfigured_locations"]
    for location, result in built.results.items():
        assert result.counters["items_received"] == len(built.sku_ids(location))


def test_unknown_configured_location_fails_that_location(tmp_path):
    b = Built(tmp_path, ["us-esat1", "us-east1"])
    assert not b.results["us-esat1"].ok
    assert "unknown to the source" in b.results["us-esat1"].failure_reason()
    assert b.results["us-east1"].ok and b.results["global"].ok


def test_snapshot_date_and_location_columns(built):
    rows = built.rows("us-east1", "sku_price_tier")
    assert {r["snapshot_date"] for r in rows} == {dt.date(2026, 10, 30)}
    assert {r["location"] for r in rows} == {"us-east1"}


def _edited_catalog(tmp_path, edit):
    """Copies of the fixture pages, with `edit(price)` applied to every price entry."""
    folder = tmp_path / "catalog"
    folder.mkdir()
    for path in _files():
        with open(path) as fh:
            page = json.load(fh)
        for price in page.get("prices") or []:
            edit(price)
        (folder / os.path.basename(path)).write_text(json.dumps(page))
    return sorted(str(p) for p in folder.iterdir())


def _transform(files, tmp_path, location):
    transform = GcpTransform(_settings(), [location, "global"])
    return transform.transform_location(transform.load(files), location, str(tmp_path / location), RUN, DATE)


def test_other_currency_list_price_is_null_and_counted(tmp_path):
    """Edge Cases (other currency), FR-017: never converted (feature 004 T057)."""

    def to_euro(price):
        if price["name"].startswith(f"skus/{N2_CORE}/"):
            price["currencyCode"] = "EUR"
            for entry in price.get("skuPrices") or []:
                for tier in (entry.get("rate") or {}).get("tiers") or []:
                    tier["listPrice"]["currencyCode"] = "EUR"

    result = _transform(_edited_catalog(tmp_path, to_euro), tmp_path, "us-east1")
    assert result.ok
    tables = {f.table: pq.read_table(f.local_path).to_pylist() for f in result.files}
    core = [t for t in tables["sku_price_tier"] if t["sku_id"] == N2_CORE]
    assert len(core) == 3 and all(t["list_price"] is None and t["currency_code"] == "EUR" for t in core)
    others = [t for t in tables["sku_price_tier"] if t["sku_id"] != N2_CORE]
    assert any(t["list_price"] is not None for t in others)
    baseline = _transform(_files(), tmp_path / "baseline", "us-east1")
    assert result.unparseable_prices == baseline.unparseable_prices + 3


def test_location_with_no_readable_list_price_fails(tmp_path):
    """Edge Cases (a required field disappears): the location fails with a reason (T058)."""

    def drop_list_prices(price):
        for entry in price.get("skuPrices") or []:
            for tier in (entry.get("rate") or {}).get("tiers") or []:
                tier.pop("listPrice", None)

    files = _edited_catalog(tmp_path, drop_list_prices)
    result = _transform(files, tmp_path, "us-east1")
    assert not result.ok
    assert "has a readable USD list price" in result.failure_reason()
    assert result.unparseable_prices > 0

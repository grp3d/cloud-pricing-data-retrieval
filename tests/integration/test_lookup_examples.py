"""
The documented lookups (data-model.md, contracts/storage-layout.md § Lookup examples)
work on tables published by real-shaped runs (US3; FR-016, FR-019, SC-002; feature 004
T037). Uses only documented columns, as cloud-pricing-app will.
"""

from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from src.pipeline import manifest as m
from tests.integration import test_run_snapshot_azure as azure_run
from tests.integration import test_run_snapshot_gcp as gcp_run


def _read(store, tmp_path, provider, table, location):
    current = m.read_current(store, provider, azure_run.DATE)
    rows = []
    for i, f in enumerate(current.tables[table].regions[location].files):
        local = tmp_path / f"{provider}-{table}-{location}-{i}.parquet"
        store.download_file(f.path, str(local))
        rows += pq.read_table(local).to_pylist()
    return rows


@pytest.fixture
def published(local_store, tmp_path):
    for name, module in (("azure", azure_run), ("gcp", gcp_run)):
        (tmp_path / name).mkdir()
        module.run(local_store, tmp_path / name)
    return local_store


def test_azure_vm_linux_on_demand(published, tmp_path):
    rows = [
        r
        for r in _read(published, tmp_path, "azure", "price_item", "eastus")
        if r["arm_sku_name"] == "Standard_D4s_v5"
        and r["price_type"] == "Consumption"
        and r["service_name"] == "Virtual Machines"
        and "Windows" not in r["product_name"]
        and "Spot" not in r["sku_name"]
        and "Low Priority" not in r["sku_name"]
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["unit_of_measure"] == "1 Hour" and row["currency_code"] == "USD"
    assert row["unit_price"] == 0.192


def test_gcp_n2_standard_4_on_demand_and_cud(published, tmp_path):
    skus = _read(published, tmp_path, "gcp", "sku", "us-east1")
    prices = _read(published, tmp_path, "gcp", "sku_price", "us-east1")
    tiers = _read(published, tmp_path, "gcp", "sku_price_tier", "us-east1")

    def sku_for(prefix):
        found = [
            s for s in skus
            if s["product_taxonomy"] and "VMs On Demand" in s["product_taxonomy"] and "N2" in s["product_taxonomy"]
            and s["display_name"].startswith(prefix)
        ]
        assert len(found) == 1
        return found[0]["sku_id"]

    def price(sku_id, description):
        model = [p for p in prices if p["sku_id"] == sku_id and p["consumption_model_description"] == description]
        assert len(model) == 1
        tier = [
            t for t in tiers
            if t["sku_id"] == sku_id and t["consumption_model"] == model[0]["consumption_model"] and t["start_amount"] == 0
        ]
        assert len(tier) == 1
        return tier[0]["list_price"]

    core, ram = sku_for("N2 Instance Core"), sku_for("N2 Instance Ram")
    hourly = 4 * price(core, "Default") + 16 * price(ram, "Default")
    assert price(core, "Default") == Decimal("0.031611")
    assert hourly > 0
    assert price(core, "Compute Flexible CUD - 1 Year") < price(core, "Default")


def test_tiers_have_lower_bounds(published, tmp_path):
    tiers = sorted(
        r["tier_minimum_units"]
        for r in _read(published, tmp_path, "azure", "price_item", "eastus")
        if r["meter_name"] == "Hot RA-GZRS Data Stored"
    )
    assert tiers == [0.0, 51200.0, 512000.0]


def test_price_types_are_distinguishable_by_columns(published, tmp_path):
    items = _read(published, tmp_path, "azure", "price_item", "eastus")
    assert {"Consumption", "DevTestConsumption", "Reservation"} <= {r["price_type"] for r in items}
    assert {r["reservation_term"] for r in items if r["price_type"] == "Reservation"} == {"1 Year", "3 Years"}
    plans = _read(published, tmp_path, "azure", "price_item_savings_plan", "eastus")
    assert {p["term"] for p in plans} == {"1 Year", "3 Years"}
    gcp_prices = _read(published, tmp_path, "gcp", "sku_price", "us-east1")
    assert {"Default", "Compute Flexible CUD - 1 Year", "Compute Flexible CUD - 3 Year"} <= {
        p["consumption_model_description"] for p in gcp_prices
    }

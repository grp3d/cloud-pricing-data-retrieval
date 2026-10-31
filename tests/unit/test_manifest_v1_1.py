"""
Manifest 1.1 (data-model.md § Manifest 1.1 additions; FR-010–FR-012, FR-031, FR-032,
FR-034, FR-035; feature 004 T015). Test-first.
"""

import datetime as dt
import json

import pytest

from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from src.pipeline.providers import get_provider
from tests.helpers.contracts import validate_manifest_doc

DATE = "2026-10-05"
RUN = "20261005T130000Z-111111"
T0 = dt.datetime(2026, 10, 5, 13, 0, 0, tzinfo=dt.timezone.utc)
SETTINGS = PipelineSettings.from_env({})


def _tables(provider, location):
    return {
        table: m.RegionTableEntry(
            RUN,
            4,
            [
                m.DataFile(
                    f"{provider}/parquet/{table}/snapshot_date={DATE}/region={location}/part-{RUN}.parquet",
                    10,
                    "a" * 64,
                    4,
                )
            ],
        )
        for table in get_provider(provider).table_names
    }


def _ok(provider, location, raw_unit=None, **counters):
    raw = None
    if raw_unit is not None:
        raw = m.make_raw_entry(f"{provider}/raw/{DATE}/{raw_unit}/{RUN}/", 3, 900, T0, 30)
    return m.RegionOutcome(location, True, 1, tables=_tables(provider, location), raw=raw, **counters)


def _build(provider, outcomes, requested, previous=None, **extra):
    return m.build_revision(
        previous,
        provider=provider,
        snapshot_date=DATE,
        run_id=RUN,
        run_info=m.new_run_info(SETTINGS, "manual", "full", T0, T0),
        outcomes=outcomes,
        requested_regions=requested,
        created_at=T0,
        **extra,
    )


@pytest.mark.parametrize("provider", ["aws", "azure", "gcp"])
def test_version_and_tables_come_from_the_provider(provider):
    location = {"aws": "us-east-1", "azure": "eastus", "gcp": "us-east1"}[provider]
    man = _build(provider, [_ok(provider, location, raw_unit=location)], [location])
    assert man.manifest_version == "1.1"
    spec = get_provider(provider)
    assert list(man.tables) == list(spec.table_names)
    assert {name: t.schema_version for name, t in man.tables.items()} == spec.tables
    validate_manifest_doc(json.loads(man.to_json()))


def test_aws_output_unchanged_apart_from_the_version():
    man = _build("aws", [_ok("aws", "us-east-1", raw_unit="us-east-1")], ["us-east-1"])
    doc = man.to_dict()
    assert "catalog" not in doc and "excluded_price_types" not in doc
    result = doc["run"]["region_results"][0]
    assert set(result) == {
        "region", "outcome", "attempts", "services_downloaded", "services_without_price_list", "unparseable_prices"
    }


def test_new_region_counters_serialize_only_when_set():
    outcome = _ok(
        "azure",
        "eastus",
        raw_unit="eastus",
        items_received=25,
        duplicates_removed=1,
        unlocated_rows=0,
        throttled_responses=2,
        requests_made=4,
    )
    man = _build("azure", [outcome, _ok("azure", "global", raw_unit="global", items_received=8, unlocated_rows=4)],
                 ["eastus", "global"], excluded_price_types=["DevTestConsumption"])
    doc = json.loads(man.to_json())
    results = {r["region"]: r for r in doc["run"]["region_results"]}
    assert results["eastus"]["items_received"] == 25
    assert results["eastus"]["duplicates_removed"] == 1
    assert results["eastus"]["throttled_responses"] == 2
    assert results["eastus"]["requests_made"] == 4
    assert "requests_made" not in results["global"]
    assert results["global"]["unlocated_rows"] == 4
    assert doc["excluded_price_types"] == ["DevTestConsumption"]
    validate_manifest_doc(doc)
    again = m.Manifest.from_json(man.to_json())
    assert again.to_dict() == doc


def test_catalog_block_round_trips():
    catalog = m.CatalogInfo(
        api_version="v2beta",
        requests_made=43,
        throttled_responses=0,
        skus_received=104224,
        skus_out_of_scope=98,
        skus_unconfigured_locations=60000,
        skus_unlocated=448,
        skus_without_price=0,
        prices_without_sku=0,
    )
    man = _build(
        "gcp",
        [_ok("gcp", "us-east1"), _ok("gcp", "global")],
        ["us-east1", "global"],
        catalog=catalog,
        raw={"catalog": m.make_raw_entry(f"gcp/raw/{DATE}/catalog/{RUN}/", 43, 9000, T0, 30)},
    )
    doc = json.loads(man.to_json())
    assert doc["catalog"]["requests_made"] == 43
    assert doc["catalog"]["api_version"] == "v2beta"
    assert list(doc["raw"]) == ["catalog"]
    validate_manifest_doc(doc)
    again = m.Manifest.from_json(man.to_json())
    assert again.catalog == catalog
    assert again.to_dict() == doc


def test_global_counts_as_requested_location():
    man = _build("azure", [_ok("azure", "eastus", raw_unit="eastus"), m.RegionOutcome("global", False, 3, reason="HTTP 503")],
                 ["eastus", "global"])
    assert man.status == "partial"
    assert [f.region for f in man.failed] == ["global"]


def test_failed_catalog_fails_every_location():
    reason = "catalog download failed: HTTP 503 after 4 attempt(s)"
    man = _build("gcp", [m.RegionOutcome(loc, False, 4, reason=reason) for loc in ["us-east1", "global"]],
                 ["us-east1", "global"], catalog=m.CatalogInfo(api_version="v2beta", requests_made=8, skus_received=0))
    assert man.status == "failed"
    assert {f.reason for f in man.failed} == {reason}
    validate_manifest_doc(json.loads(man.to_json()))


def test_carried_forward_catalog_raw_entry_is_kept():
    first = _build(
        "gcp",
        [_ok("gcp", "us-east1"), _ok("gcp", "global")],
        ["us-east1", "global"],
        raw={"catalog": m.make_raw_entry(f"gcp/raw/{DATE}/catalog/{RUN}/", 43, 9000, T0, 30)},
    )
    second = m.build_revision(
        first,
        provider="gcp",
        snapshot_date=DATE,
        run_id="20261005T140000Z-222222",
        run_info=m.new_run_info(SETTINGS, "manual", "transform-only", T0, T0),
        outcomes=[_ok("gcp", "us-east1"), _ok("gcp", "global")],
        requested_regions=["us-east1", "global"],
        created_at=T0,
    )
    assert list(second.raw) == ["catalog"]

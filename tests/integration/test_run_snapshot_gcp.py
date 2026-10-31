"""
End-to-end GCP snapshot runs (US2; FR-003, FR-006, FR-013, FR-015, FR-031, SC-004;
feature 004 T031), against local and (moto) S3 storage. The real GCP downloader runs over
the recorded fixtures through FakeHttpSource, so no network or real key is used.
"""

import datetime as dt
import json

from click.testing import CliRunner

from src.gcp.downloader import GcpCatalogDownloader
from src.pipeline import __main__ as cli_module
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from src.pipeline.notify import RUN_FAILED, Notifier
from src.pipeline.runner import RunRequest, run_snapshot
from src.pipeline.verify import verify_snapshot
from tests.helpers.contracts import assert_valid_contract
from tests.helpers.fake_pricing import http_error
from tests.unit.test_gcp_api import catalog_routes, listing_urls

DATE = "2026-10-30"
T0 = dt.datetime(2026, 10, 30, 13, 0, 0, tzinfo=dt.timezone.utc)
KEY = "TEST-KEY-7f3a9c"
N2_CORE = "BB77-5FDA-69D9"


def settings_for(store, **env):
    base = {
        "PIPELINE_STORAGE_URI": store.uri,
        "PIPELINE_PROVIDER": "gcp",
        "GCP_PRICING_API_KEY": KEY,
        "PRICING_REGIONS": "us-east1,us-central1",
        "PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS": "0",
        "PRICING_DOWNLOAD_RETRY_MAX_RETRIES": "1",
        "PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND": "1000",
    }
    base.update(env)
    return PipelineSettings.from_env(base)


def run(store, tmp_path, settings=None, fake=None, **kwargs):
    settings = settings or settings_for(store)
    fake = fake or catalog_routes(settings)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    notifier = Notifier(settings)
    report = run_snapshot(
        settings,
        RunRequest(snapshot_date=DATE, trigger="manual", **kwargs),
        store=store,
        downloader=GcpCatalogDownloader(settings, fetch=fake, sleep=lambda s: None),
        now=lambda: T0,
        sleep=lambda s: None,
        work_dir=str(work),
        notifier=notifier,
    )
    return report, fake, notifier


def keys(store, prefix):
    return sorted(o.key for o in store.list(prefix))


def test_full_run(store, tmp_path):
    report, fake, _ = run(store, tmp_path)
    assert report.snapshot_status == "succeeded"
    current = m.read_current(store, "gcp", DATE)
    assert current.requested == ["global", "us-central1", "us-east1"]
    assert list(current.tables) == ["service", "sku", "sku_price", "sku_price_tier"]
    assert list(current.raw) == ["catalog"]
    assert keys(store, f"gcp/raw/{DATE}/") == sorted(
        f"gcp/raw/{DATE}/catalog/{report.run_id}/{name}.zst"
        for name in ("services-p0001.json", "skus-p0001.json", "skus-p0002.json", "prices-p0001.json", "prices-p0002.json")
    )
    doc = json.loads(store.get_bytes(f"gcp/manifests/{DATE}/manifest.json"))
    assert doc["catalog"]["api_version"] == "v2beta"
    assert doc["catalog"]["requests_made"] == 5 == len(fake.requests)
    assert json.loads(store.get_bytes("gcp/manifests/latest.json"))["snapshot_date"] == DATE
    assert verify_snapshot(store, "gcp") == []
    assert_valid_contract(store, "gcp")
    assert current.tables["service"].regions["us-east1"].row_count == 0
    assert current.tables["service"].regions["global"].row_count > 0


def test_multi_region_sku_filed_under_both_regions(store, tmp_path):
    import pyarrow.parquet as pq

    report, _, _ = run(store, tmp_path)
    current = m.read_current(store, "gcp", DATE)
    found = {}
    for location in ("us-east1", "us-central1"):
        f = current.tables["sku"].regions[location].files[0]
        local = tmp_path / f"{location}.parquet"
        store.download_file(f.path, str(local))
        rows = [r for r in pq.read_table(local).to_pylist() if r["sku_id"] == N2_CORE]
        assert len(rows) == 1
        found[location] = rows[0]["geo_regions"]
    assert found["us-east1"] == found["us-central1"] == ["us-central1", "us-east1", "us-west1"]


def test_sku_accounting(store, tmp_path):
    import pyarrow.parquet as pq

    run(store, tmp_path)
    current = m.read_current(store, "gcp", DATE)
    stored = set()
    for location, entry in current.tables["sku"].regions.items():
        local = tmp_path / f"sku-{location}.parquet"
        store.download_file(entry.files[0].path, str(local))
        stored |= set(pq.read_table(local).column("sku_id").to_pylist())
    c = current.catalog
    assert c.skus_received == len(stored) + c.skus_out_of_scope + c.skus_unconfigured_locations
    assert c.prices_without_sku == 1


def test_catalog_page_that_keeps_failing_fails_the_snapshot(store, tmp_path):
    settings = settings_for(store)
    second = listing_urls(settings)["prices"] + "&pageToken=fixture-prices-p0002"
    fake = catalog_routes(settings).fail(second, http_error(503))
    report, _, notifier = run(store, tmp_path, settings=settings, fake=fake)
    assert report.snapshot_status == "failed"
    current = m.read_current(store, "gcp", DATE)
    assert {f.region for f in current.failed} == {"global", "us-central1", "us-east1"}
    assert all("catalog download failed" in f.reason and "503" in f.reason for f in current.failed)
    assert store.head("gcp/manifests/latest.json") is None
    assert RUN_FAILED in report.alerts_sent
    assert "gcp" in notifier.format(RUN_FAILED, report)[0]
    assert_valid_contract(store, "gcp")


def test_rejected_key_fails_without_retries(store, tmp_path):
    settings = settings_for(store)
    body = b'{"error": {"code": 400, "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}'
    fake = catalog_routes(settings).fail(listing_urls(settings)["services"], http_error(400, body=body))
    report, fake, _ = run(store, tmp_path, settings=settings, fake=fake)
    assert report.snapshot_status == "failed"
    assert len(fake.requests) == 1
    current = m.read_current(store, "gcp", DATE)
    assert all("API_KEY_INVALID" in f.reason for f in current.failed)


def test_missing_key_exits_2_before_anything_is_written(tmp_path):
    root = tmp_path / "store"
    result = CliRunner().invoke(
        cli_module.cli, ["run"], env={"PIPELINE_PROVIDER": "gcp", "PIPELINE_STORAGE_URI": f"file://{root}"},
        catch_exceptions=False,
    )
    assert result.exit_code == 2
    assert "GCP_PRICING_API_KEY" in result.output
    assert not root.exists()


def test_transform_only_rebuilds_from_stored_catalog(store, tmp_path):
    first, _, _ = run(store, tmp_path)
    settings = settings_for(store)
    fake = catalog_routes(settings)
    second, fake, _ = run(store, tmp_path, settings=settings, fake=fake, transform_only=True)
    assert second.snapshot_status == "succeeded" and second.revision == 2
    assert fake.requests == []
    assert first.row_counts == second.row_counts
    current = m.read_current(store, "gcp", DATE)
    assert current.catalog.requests_made == 0

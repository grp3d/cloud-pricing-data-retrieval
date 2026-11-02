"""
End-to-end Azure snapshot runs (US1; FR-002, FR-009–FR-014, FR-020, FR-021, FR-035,
SC-004; feature 004 T024), against local and (moto) S3 storage. The real Azure downloader
runs over the recorded fixtures through FakeHttpSource, so no network is used.
"""

import datetime as dt
import json
from urllib.parse import unquote

import pytest

from src.azure.api import listing_url
from src.azure.downloader import AzurePricesDownloader
from src.pipeline import claims, layout
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from src.pipeline.notify import RUN_PARTIAL, Notifier
from src.pipeline.runner import RunRequest, run_snapshot
from src.pipeline.verify import verify_snapshot
from tests.helpers.contracts import assert_valid_contract
from tests.helpers.fake_pricing import AZURE_FIXTURES, FakePricingSource, FakeHttpSource, http_error

DATE = "2026-10-30"
T0 = dt.datetime(2026, 10, 30, 13, 0, 0, tzinfo=dt.timezone.utc)
EASTUS_P2 = (
    "https://prices.azure.com:443/api/retail/prices?api-version=2023-01-01-preview"
    "&$filter=armRegionName%20eq%20%27eastus%27&$skip=1000"
)


def settings_for(store, **env):
    base = {
        "PIPELINE_STORAGE_URI": store.uri,
        "PIPELINE_PROVIDER": "azure",
        "PRICING_REGIONS": "eastus,north-america",
        "PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS": "0",
        "PRICING_DOWNLOAD_RETRY_MAX_RETRIES": "1",
        "PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND": "1000",
    }
    base.update(env)
    return PipelineSettings.from_env(base)


def source_for(settings):
    fake = FakeHttpSource()
    fx = lambda n: f"{AZURE_FIXTURES}/{n}"  # noqa: E731
    fake.route(listing_url(settings, "eastus"), fx("eastus-p0001.json"))
    fake.route(EASTUS_P2, fx("eastus-p0002.json"))  # the recorded page 1 links here
    fake.route(listing_url(settings, "north-america"), fx("north-america-p0001.json"))
    fake.route(listing_url(settings, "global", source_value=""), fx("global-empty-p0001.json"))
    fake.route(listing_url(settings, "global", source_value="Global"), fx("global-global-p0001.json"))
    return fake


def run(store, tmp_path, settings=None, fake=None, regions=None, log=None, **kwargs):
    settings = settings or settings_for(store)
    fake = fake or source_for(settings)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    notifier = Notifier(settings)
    report = run_snapshot(
        settings,
        RunRequest(snapshot_date=DATE, regions=regions, trigger="manual", **kwargs),
        store=store,
        downloader=AzurePricesDownloader(settings, fetch=fake, sleep=lambda s: None),
        now=lambda: T0,
        sleep=lambda s: None,
        work_dir=str(work),
        notifier=notifier,
        log=log,
    )
    return report, fake, notifier


def keys(store, prefix):
    return sorted(o.key for o in store.list(prefix))


def test_full_run(store, tmp_path):
    report, fake, _ = run(store, tmp_path)
    assert report.snapshot_status == "succeeded"
    current = m.read_current(store, "azure", DATE)
    assert current.requested == ["eastus", "global", "north-america"]
    assert current.manifest_version == "1.1"
    assert list(current.tables) == ["price_item", "price_item_savings_plan"]
    run_id = report.run_id
    assert keys(store, "azure/parquet/") == sorted(
        layout.parquet_file_key("azure", t, DATE, loc, run_id)
        for t in ("price_item", "price_item_savings_plan")
        for loc in ("eastus", "global", "north-america")
    )
    assert keys(store, f"azure/raw/{DATE}/eastus/") == [
        f"azure/raw/{DATE}/eastus/{run_id}/prices-p0001.json.zst",
        f"azure/raw/{DATE}/eastus/{run_id}/prices-p0002.json.zst",
    ]
    assert keys(store, f"azure/raw/{DATE}/global/") == [
        f"azure/raw/{DATE}/global/{run_id}/prices-empty-p0001.json.zst",
        f"azure/raw/{DATE}/global/{run_id}/prices-global-p0001.json.zst",
    ]
    assert json.loads(store.get_bytes("azure/manifests/latest.json"))["snapshot_date"] == DATE
    assert verify_snapshot(store, "azure") == []
    assert_valid_contract(store, "azure")
    assert not keys(store, "aws/")
    assert "excluded_price_types" not in json.loads(store.get_bytes(f"azure/manifests/{DATE}/manifest.json"))

    results = {r.region: r for r in current.run.region_results}
    assert results["eastus"].requests_made == 2
    assert results["global"].requests_made == 2
    assert results["global"].unlocated_rows == 4
    # SC-004: every received item is stored or counted as a duplicate.
    for loc, r in results.items():
        stored = current.tables["price_item"].regions[loc].row_count
        assert r.items_received == stored + (r.duplicates_removed or 0)
    assert results["eastus"].duplicates_removed == 1


def test_failing_location_makes_a_partial_snapshot_and_rerun_completes_it(store, tmp_path):
    settings = settings_for(store)
    fake = source_for(settings).fail(EASTUS_P2, http_error(503))
    report, _, notifier = run(store, tmp_path, settings=settings, fake=fake)
    assert report.snapshot_status == "partial"
    current = m.read_current(store, "azure", DATE)
    assert [f.region for f in current.failed] == ["eastus"]
    assert "503" in current.failed[0].reason
    assert store.head("azure/manifests/latest.json") is None
    assert RUN_PARTIAL in report.alerts_sent
    subject, _ = notifier.format(RUN_PARTIAL, report)
    assert "azure" in subject

    rerun, _, _ = run(store, tmp_path, regions=["eastus"])
    assert rerun.snapshot_status == "succeeded" and rerun.revision == 2
    current = m.read_current(store, "azure", DATE)
    assert current.succeeded == ["eastus", "global", "north-america"]
    paths = {f.path for r in current.tables["price_item"].regions.values() for f in r.files}
    assert any(rerun.run_id in p for p in paths) and any(report.run_id in p for p in paths)
    assert verify_snapshot(store, "azure") == []


def test_devtest_exclusion_is_recorded(store, tmp_path):
    settings = settings_for(store, INCLUDE_AZURE_DEVTEST_PRICES="false")
    report, fake, _ = run(store, tmp_path, settings=settings)
    assert report.snapshot_status == "succeeded"
    doc = json.loads(store.get_bytes(f"azure/manifests/{DATE}/manifest.json"))
    assert doc["excluded_price_types"] == ["DevTestConsumption"]
    first_pages = [u for u in fake.urls() if "$skip" not in u]
    assert len(first_pages) == 4 and all("type ne 'DevTestConsumption'" in unquote(u) for u in first_pages)
    assert_valid_contract(store, "azure")


def test_an_azure_claim_does_not_block_aws(store, tmp_path):
    aws_settings = PipelineSettings.from_env({"PIPELINE_STORAGE_URI": store.uri, "PRICING_REGIONS": "us-east-1"})
    with claims.held_claim(store, "azure", DATE, "20261030T130000Z-aaaaaa", "manual", 180, T0):
        work = tmp_path / "aws-work"
        work.mkdir()
        report = run_snapshot(
            aws_settings,
            RunRequest(snapshot_date=DATE, trigger="manual"),
            store=store,
            downloader=FakePricingSource(),
            now=lambda: T0,
            sleep=lambda s: None,
            work_dir=str(work),
        )
    assert report.outcome == "completed" and report.snapshot_status == "succeeded"


def test_progress_is_logged_as_each_location_finishes(store, tmp_path):
    lines = []
    report, _, _ = run(store, tmp_path, log=lines.append)
    assert lines[0].startswith(f"azure {DATE}: run {report.run_id} started (full), 3 location(s): ")
    done = [line for line in lines if ": done (" in line]
    assert len(done) == 3
    assert sorted(line.split(":")[0] for line in done) == ["azure eastus", "azure global", "azure north-america"]
    assert {line.split("(")[1].split(")")[0] for line in done} == {"1/3", "2/3", "3/3"}
    assert lines[-1] == f"azure {DATE}: published revision 1 (succeeded)"


def test_a_failed_location_is_logged_with_its_reason(store, tmp_path):
    settings = settings_for(store)
    fake = source_for(settings).fail(EASTUS_P2, http_error(503))
    lines = []
    run(store, tmp_path, settings=settings, fake=fake, log=lines.append)
    failed = [line for line in lines if line.startswith("azure eastus: failed (")]
    assert len(failed) == 1 and "503" in failed[0]
    assert lines[-1] == f"azure {DATE}: published revision 1 (partial)"


def test_progress_goes_to_the_log_by_default(store, tmp_path, caplog):
    caplog.set_level("INFO", logger="src.pipeline.runner")
    report, _, _ = run(store, tmp_path)
    messages = [r.getMessage() for r in caplog.records if r.name == "src.pipeline.runner"]
    assert any(msg.startswith(f"azure {DATE}: run {report.run_id} started") for msg in messages)
    assert f"azure {DATE}: published revision 1 (succeeded)" in messages

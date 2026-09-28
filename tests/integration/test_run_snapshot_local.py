"""
End-to-end snapshot runs against a local storage root with the fake downloader
(US2; FR-003, FR-009, FR-013–FR-015, FR-019; quickstart A1).

The scenarios are shared with test_run_snapshot_s3.py through `run_scenarios`.
"""

import datetime as dt
import json
import os
import re

import pytest

from src.pipeline import layout
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from src.pipeline.runner import RunRequest, run_snapshot
from src.pipeline.verify import verify_snapshot
from tests.helpers.contracts import assert_valid_contract
from tests.helpers.fake_pricing import FakePricingSource

DATE = "2026-10-05"
REGIONS = ["us-east-1", "eu-west-1"]
T0 = dt.datetime(2026, 10, 5, 13, 0, 0, tzinfo=dt.timezone.utc)


def settings_for(store, **env):
    base = {"PIPELINE_STORAGE_URI": store.uri, "PRICING_REGIONS": ",".join(REGIONS)}
    base.update(env)
    return PipelineSettings.from_env(base)


def run(store, source, tmp_path, regions=None, clock=None, **kwargs):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return run_snapshot(
        settings_for(store),
        RunRequest(snapshot_date=DATE, regions=regions, trigger="manual", **kwargs),
        store=store,
        downloader=source,
        now=clock or (lambda: T0),
        sleep=lambda s: None,
        work_dir=str(work),
    )


def keys(store, prefix="aws/"):
    return sorted(o.key for o in store.list(prefix))


# --- scenarios (shared with the S3 variant) ------------------------------------------

def scenario_full_run_layout(store, tmp_path):
    report = run(store, FakePricingSource(), tmp_path)
    assert report.outcome == "completed"
    assert report.snapshot_status == "succeeded"
    run_id = report.run_id

    all_keys = keys(store)
    raw = [k for k in all_keys if k.startswith("aws/raw/")]
    assert raw == sorted(
        f"aws/raw/{DATE}/{region}/{run_id}/pricing-{svc}-{region}.json.zst"
        for region in REGIONS
        for svc in ("AmazonEC2", "AmazonS3")
    )
    parquet = [k for k in all_keys if k.startswith("aws/parquet/")]
    assert parquet == sorted(
        layout.parquet_file_key("aws", table, DATE, region, run_id)
        for table in layout.TABLES
        for region in REGIONS
    )
    assert [k for k in all_keys if k.startswith("aws/manifests/")] == [
        f"aws/manifests/{DATE}/manifest.json",
        f"aws/manifests/{DATE}/revisions/0001.json",
        "aws/manifests/latest.json",
    ]
    assert store.get_bytes(f"aws/manifests/{DATE}/manifest.json") == store.get_bytes(
        f"aws/manifests/{DATE}/revisions/0001.json"
    )
    current = m.read_current(store, "aws", DATE)
    assert current.status == "succeeded" and current.revision == 1
    assert json.loads(store.get_bytes("aws/manifests/latest.json"))["snapshot_date"] == DATE
    assert verify_snapshot(store, "aws") == []
    # No marker or lock files anywhere (FR-019); the run claim is released.
    assert not [k for k in all_keys if re.search(r"(_SUCCESS|_REGION_COMPLETE|\.locks)", k)]
    assert not [k for k in all_keys if k.startswith("aws/claims/")]
    assert_valid_contract(store)


def scenario_partial_run(store, tmp_path):
    report = run(store, FakePricingSource({"eu-west-1": "permanent_error"}), tmp_path)
    assert report.snapshot_status == "partial"
    current = m.read_current(store, "aws", DATE)
    assert current.status == "partial"
    assert current.succeeded == ["us-east-1"]
    assert [f.region for f in current.failed] == ["eu-west-1"]
    assert "403" in current.failed[0].reason
    assert store.head("aws/manifests/latest.json") is None
    assert_valid_contract(store)


def scenario_all_regions_fail(store, tmp_path):
    report = run(store, FakePricingSource(default="permanent_error"), tmp_path)
    assert report.snapshot_status == "failed"
    current = m.read_current(store, "aws", DATE)
    assert current.status == "failed"
    assert current.tables == {}
    assert store.head("aws/manifests/latest.json") is None
    assert not [k for k in keys(store) if k.startswith("aws/parquet/")]
    assert_valid_contract(store)


def scenario_staging_cleaned_up(store, tmp_path):
    run(store, FakePricingSource(), tmp_path)
    leftovers = [name for _, _, files in os.walk(tmp_path / "work") for name in files]
    assert leftovers == []


SCENARIOS = [
    scenario_full_run_layout,
    scenario_partial_run,
    scenario_all_regions_fail,
    scenario_staging_cleaned_up,
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
def test_local(scenario, local_store, tmp_path):
    scenario(local_store, tmp_path)


# --- US4: targeted repair (FR-004, FR-016, FR-043) ------------------------------------

from click.testing import CliRunner  # noqa: E402

from tests.helpers.fake_pricing import fail_times  # noqa: E402


def test_partial_then_region_rerun_succeeds(local_store, tmp_path):
    source = FakePricingSource({"eu-west-1": fail_times(1)})
    first = run(local_store, source, tmp_path)
    assert first.snapshot_status == "partial"
    rev1 = m.read_current(local_store, "aws", DATE)

    second = run(local_store, source, tmp_path, regions=["eu-west-1"])
    assert second.mode == "regions"
    # Only the failed region is downloaded again.
    assert source.calls.count("us-east-1") == 1 and source.calls.count("eu-west-1") == 2
    rev2 = m.read_current(local_store, "aws", DATE)
    assert rev2.revision == 2 and rev2.status == "succeeded"
    us = rev2.tables["price_fact"].regions["us-east-1"]
    assert us.written_by_run == first.run_id
    assert us.files == rev1.tables["price_fact"].regions["us-east-1"].files
    assert rev2.tables["price_fact"].regions["eu-west-1"].written_by_run == second.run_id
    assert json.loads(local_store.get_bytes("aws/manifests/latest.json"))["revision"] == 2
    assert_valid_contract(local_store)


def test_transform_only_rebuilds_without_download(local_store, tmp_path):
    first = run(local_store, FakePricingSource(), tmp_path)
    source = FakePricingSource()
    second = run(local_store, source, tmp_path, transform_only=True, skip_retention=True)
    assert source.calls == []
    assert second.mode == "transform-only"
    rev2 = m.read_current(local_store, "aws", DATE)
    assert rev2.revision == 2 and rev2.status == "succeeded"
    new_paths = m.active_file_paths(rev2)
    assert all(second.run_id in p for p in new_paths)
    # Old files remain until retention removes them after the grace period.
    old_file = layout.parquet_file_key("aws", "price_fact", DATE, "us-east-1", first.run_id)
    assert local_store.head(old_file)
    # Raw data is reused, not re-stored.
    assert rev2.raw["us-east-1"].location.endswith(f"/{first.run_id}/")
    assert_valid_contract(local_store)

    # With inline retention (the default), the next run cleans them up.
    run(local_store, FakePricingSource(), tmp_path, transform_only=True)
    assert local_store.head(old_file) is None


def test_transform_only_with_purged_raw_exits_3(local_store, tmp_path, monkeypatch):
    run(local_store, FakePricingSource(), tmp_path)
    for obj in list(local_store.list("aws/raw/")):
        local_store.delete(obj.key)
    before = keys(local_store)
    monkeypatch.setenv("PIPELINE_STORAGE_URI", local_store.uri)
    monkeypatch.setenv("PRICING_REGIONS", ",".join(REGIONS))
    from src.pipeline.__main__ import cli

    result = CliRunner().invoke(cli, ["run", "--snapshot-date", DATE, "--transform-only"])
    assert result.exit_code == 3
    assert keys(local_store) == before


def test_rerun_failure_keeps_good_data(local_store, tmp_path):
    first = run(local_store, FakePricingSource(), tmp_path)
    second = run(local_store, FakePricingSource({"us-east-1": "permanent_error"}), tmp_path, regions=["us-east-1"])
    rev2 = m.read_current(local_store, "aws", DATE)
    assert rev2.status == "succeeded"
    assert rev2.tables["price_fact"].regions["us-east-1"].written_by_run == first.run_id
    assert second.region_results[0]["outcome"] == "failed"


def test_run_mode_recorded(local_store, tmp_path):
    run(local_store, FakePricingSource(), tmp_path)
    assert m.read_current(local_store, "aws", DATE).run.mode == "full"
    run(local_store, FakePricingSource(), tmp_path, regions=["us-east-1"])
    assert m.read_current(local_store, "aws", DATE).run.mode == "regions"
    run(local_store, FakePricingSource(), tmp_path, transform_only=True)
    assert m.read_current(local_store, "aws", DATE).run.mode == "transform-only"


# --- US5: inline retention (FR-048–FR-050) ---------------------------------------------


class _Notes:
    def __init__(self):
        self.sent = []

    def send(self, kind, report, detail=None):
        self.sent.append(kind)
        return True


def test_inline_retention_cleans_superseded_after_rerun(local_store, tmp_path, no_sleep):
    first = run(local_store, FakePricingSource(), tmp_path)
    work = tmp_path / "work"
    report = run_snapshot(
        settings_for(local_store),
        RunRequest(snapshot_date=DATE, trigger="manual"),
        store=local_store,
        downloader=FakePricingSource(),
        now=lambda: T0,
        sleep=no_sleep,
        work_dir=str(work),
    )
    assert report.retention["waited_seconds"] == pytest.approx(300)
    assert report.retention["superseded_deleted"] == len(layout.TABLES) * len(REGIONS)
    assert not [k for k in keys(local_store, "aws/parquet/") if first.run_id in k]
    assert verify_snapshot(local_store, "aws") == []
    assert_valid_contract(local_store)


def test_retention_error_does_not_change_status(local_store, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("retention exploded")

    monkeypatch.setattr("src.pipeline.runner.run_retention", boom)
    notes = _Notes()
    report = run_snapshot(
        settings_for(local_store),
        RunRequest(snapshot_date=DATE, trigger="scheduled"),
        store=local_store,
        downloader=FakePricingSource(),
        now=lambda: T0,
        sleep=lambda s: None,
        work_dir=str(tmp_path),
        notifier=notes,
    )
    assert report.outcome == "completed"
    assert report.snapshot_status == "succeeded"
    assert "retention exploded" in report.retention["error"]
    assert notes.sent == ["RETENTION ERROR"]
    assert m.read_current(local_store, "aws", DATE).status == "succeeded"
    assert json.loads(local_store.get_bytes("aws/manifests/latest.json"))["snapshot_date"] == DATE
    assert_valid_contract(local_store)


def test_skip_retention_flag(local_store, tmp_path):
    run(local_store, FakePricingSource(), tmp_path)
    report = run(local_store, FakePricingSource(), tmp_path, skip_retention=True)
    assert report.retention == {"skipped": True}

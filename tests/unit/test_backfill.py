"""
upload-history (US8; FR-038–FR-042, research R18). Test-first per constitution IV: this
builds manifests for irreplaceable history.
"""

import datetime as dt
import hashlib
import json
import os

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.pipeline import backfill, claims, layout
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from tests.helpers.contracts import assert_valid_contract

DATE = "2026-06-01"
NOW = dt.datetime(2026, 10, 5, 13, 0, tzinfo=dt.timezone.utc)
REGIONS = ["eu-west-1", "us-east-1"]


def settings(store):
    return PipelineSettings.from_env({"PIPELINE_STORAGE_URI": store.uri})


def legacy_tree(root, date=DATE, regions=REGIONS, rows=3):
    """Pre-feature-003 layout: <root>/pricing_aws/parquet/<table>/snapshot_date=D/region=R/part-0.parquet."""
    for table in layout.TABLES:
        for region in regions:
            d = os.path.join(root, "pricing_aws", "parquet", table, f"snapshot_date={date}", f"region={region}")
            os.makedirs(d, exist_ok=True)
            df = pd.DataFrame({"sku": [f"{region}-{i}" for i in range(rows)], "v": range(rows)})
            pq.write_table(pa.Table.from_pandas(df, preserve_index=False), os.path.join(d, "part-0.parquet"))
            open(os.path.join(d, "_SUCCESS"), "w").close()  # legacy markers are ignored
    raw = os.path.join(root, "pricing_aws", "raw", "20260601_130000")
    os.makedirs(raw, exist_ok=True)
    with open(os.path.join(raw, "pricing-AmazonEC2-us-east-1.json"), "w") as fh:
        fh.write("{}")
    return root


def upload(store, source, **kw):
    return backfill.upload_history(store, settings(store), DATE, str(source), now=NOW, **kw)


def test_legacy_snapshot_gets_generated_manifest(store, tmp_path):
    src = legacy_tree(tmp_path / "data")
    result = upload(store, src)
    assert result["outcome"] == "uploaded" and result["source_layout"] == "legacy"
    man = m.read_current(store, "aws", DATE)
    assert man.origin == "backfill"
    assert (man.run.trigger, man.run.mode) == ("backfill", "backfill")
    assert man.raw is None
    assert man.requested == REGIONS and man.succeeded == REGIONS
    assert man.status == "succeeded" and man.revision == 1
    for table in layout.TABLES:
        for region in REGIONS:
            (f,) = man.tables[table].regions[region].files
            assert f.path == layout.parquet_file_key("aws", table, DATE, region, man.run_id)
            local = os.path.join(src, "pricing_aws", "parquet", table, f"snapshot_date={DATE}", f"region={region}", "part-0.parquet")
            data = open(local, "rb").read()
            assert (f.bytes, f.sha256, f.row_count) == (len(data), hashlib.sha256(data).hexdigest(), 3)
            assert store.get_bytes(f.path) == data
    assert json.loads(store.get_bytes("aws/manifests/latest.json"))["snapshot_date"] == DATE
    assert_valid_contract(store)


def test_never_uploads_raw(store, tmp_path):
    upload(store, legacy_tree(tmp_path / "data"))
    assert list(store.list("aws/raw/")) == []


def _local_pipeline_run(tmp_path):
    from src.pipeline.runner import RunRequest, run_snapshot
    from src.pipeline.storage import open_storage
    from tests.helpers.fake_pricing import FakePricingSource

    local_root = tmp_path / "pipeline"
    local = open_storage(f"file://{local_root}")
    report = run_snapshot(
        PipelineSettings.from_env({"PIPELINE_STORAGE_URI": local.uri, "PRICING_REGIONS": "us-east-1"}),
        RunRequest(snapshot_date=DATE),
        store=local, downloader=FakePricingSource(), now=lambda: NOW, sleep=lambda s: None,
        work_dir=str(tmp_path / "work"),
    )
    return local_root, local, report


def _files_by_identity(man):
    """(table, region, sha256, rows) for every data file: what must survive re-keying."""
    return sorted(
        (table, region, f.sha256, f.row_count)
        for table, t in man.tables.items()
        for region, r in t.regions.items()
        for f in r.files
    )


def test_new_layout_manifest_is_used_with_fresh_keys(store, tmp_path):
    """The local manifest's contents are kept, but uploaded files get a fresh run ID and new
    keys, so a published file is never overwritten (FR-043; PR review finding)."""
    local_root, local, report = _local_pipeline_run(tmp_path)
    original = m.read_current(local, "aws", DATE)

    result = upload(store, local_root)
    assert result["source_layout"] == "manifest"
    man = m.read_current(store, "aws", DATE)
    assert man.run_id != report.run_id
    assert _files_by_identity(man) == _files_by_identity(original)  # same data, same stats
    for table in man.tables.values():
        for region in table.regions.values():
            assert region.written_by_run == man.run_id
            for f in region.files:
                assert man.run_id in f.path
    assert man.raw == {}  # raw data stays local (FR-042)
    assert list(store.list("aws/raw/")) == []
    for path in m.active_file_paths(man):
        assert store.head(path).size > 0
    assert_valid_contract(store)


def test_new_layout_overwrite_never_reuses_keys(store, tmp_path):
    local_root, _, _ = _local_pipeline_run(tmp_path)
    upload(store, local_root)
    rev1 = m.read_current(store, "aws", DATE)
    before = {p: store.get_bytes(p) for p in m.active_file_paths(rev1)}

    upload(store, local_root, overwrite=True)
    rev2 = m.read_current(store, "aws", DATE)
    assert rev2.revision == 2
    assert m.active_file_paths(rev2).isdisjoint(m.active_file_paths(rev1))
    for path, data in before.items():  # revision 1's files are untouched
        assert store.get_bytes(path) == data
    assert_valid_contract(store)


def test_no_overwrite_is_rechecked_after_taking_the_claim(store, tmp_path, monkeypatch):
    """Another run may publish the date between the first check and acquiring the claim
    (PR review finding). The upload must then refuse, not overwrite."""
    src = legacy_tree(tmp_path / "data")
    real_held_claim = backfill.claims.held_claim
    published = layout.manifest_key("aws", DATE)

    def publish_then_claim(*args, **kwargs):
        store.put_bytes(published, b"published by another run")
        return real_held_claim(*args, **kwargs)

    monkeypatch.setattr(backfill.claims, "held_claim", publish_then_claim)
    with pytest.raises(backfill.BackfillPrecondition, match="already exists"):
        upload(store, src)
    assert store.get_bytes(published) == b"published by another run"
    assert list(store.list("aws/parquet/")) == []
    assert store.head(layout.claim_key("aws", DATE)) is None  # claim released


@pytest.mark.parametrize("existing", ["parquet", "manifest"])
def test_aborts_before_any_write_when_target_exists(store, tmp_path, existing):
    if existing == "parquet":
        store.put_bytes(layout.parquet_file_key("aws", "price_fact", DATE, "us-east-1", "20260601T130000Z-000000"), b"x")
    else:
        store.put_bytes(layout.manifest_key("aws", DATE), b"{}")
    before = sorted(o.key for o in store.list(""))
    with pytest.raises(backfill.BackfillPrecondition, match="already exists"):
        upload(store, legacy_tree(tmp_path / "data"))
    assert sorted(o.key for o in store.list("")) == before


def test_overwrite_publishes_new_revision_superseding_old(store, tmp_path):
    src = legacy_tree(tmp_path / "data")
    first = upload(store, src)
    rev1 = m.read_current(store, "aws", DATE)
    second = upload(store, src, overwrite=True)
    rev2 = m.read_current(store, "aws", DATE)
    assert rev2.revision == 2 and rev2.previous_revision == 1
    assert rev2.run_id != rev1.run_id
    assert m.active_file_paths(rev2).isdisjoint(m.active_file_paths(rev1))
    assert second["revision"] == 2 and first["revision"] == 1
    assert_valid_contract(store)


def test_missing_local_data_fails(store, tmp_path):
    with pytest.raises(backfill.BackfillPrecondition, match="no local table data"):
        upload(store, tmp_path / "empty")


def test_dry_run_writes_nothing(store, tmp_path):
    result = upload(store, legacy_tree(tmp_path / "data"), dry_run=True)
    assert result["outcome"] == "dry-run"
    assert result["manifest"]["origin"] == "backfill"
    assert len(result["files"]) == len(layout.TABLES) * len(REGIONS)
    assert list(store.list("")) == []


def test_held_claim_refuses(store, tmp_path):
    claims.acquire(store, "aws", DATE, "20261005T120000Z-aaaaaa", "manual", 180, NOW)
    result = upload(store, legacy_tree(tmp_path / "data"))
    assert result["outcome"] == "refused"
    assert list(store.list("aws/parquet/")) == []


def test_latest_moves_only_if_newer(store, tmp_path):
    newer = "2026-09-28"
    legacy_tree(tmp_path / "data", date=newer)
    backfill.upload_history(store, settings(store), newer, str(tmp_path / "data"), now=NOW)
    upload(store, legacy_tree(tmp_path / "data"))
    assert json.loads(store.get_bytes("aws/manifests/latest.json"))["snapshot_date"] == newer


def test_backfill_manifest_contract(tmp_path):
    from tests.helpers.contracts import validate_manifest_doc

    src = legacy_tree(tmp_path / "data")
    snap = backfill.find_local_snapshot(str(src), "aws", DATE)
    man = backfill.build_backfill_manifest(
        snap, PipelineSettings.from_env({}), run_id="20261005T130000Z-abcdef", revision=1, now=NOW
    )[0]
    validate_manifest_doc(json.loads(man.to_json()))

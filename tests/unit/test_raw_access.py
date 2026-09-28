"""Listing raw snapshots with purge dates, and downloading one (FR-025)."""

import datetime as dt

import pytest
from compression import zstd

from src.pipeline import layout, raw_access
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings

DATE = "2026-10-05"
RUN = "20261005T130000Z-111111"
T0 = dt.datetime(2026, 10, 5, 13, 30, tzinfo=dt.timezone.utc)
S = PipelineSettings.from_env({})


def _seed(store):
    for region in ("us-east-1", "eu-west-1"):
        for svc in ("AmazonEC2", "AmazonS3"):
            key = layout.raw_file_key("aws", DATE, region, RUN, f"pricing-{svc}-{region}.json")
            store.put_bytes(key, zstd.compress(f'{{"svc": "{svc}"}}'.encode()))
    outcomes = [
        m.RegionOutcome(
            region,
            True,
            1,
            tables={t: m.RegionTableEntry(RUN, 0, []) for t in layout.TABLES},
            raw=m.make_raw_entry(layout.raw_run_prefix("aws", DATE, region, RUN), 2, 60, T0, 30),
        )
        for region in ("us-east-1", "eu-west-1")
    ]
    man = m.build_revision(
        None, provider="aws", snapshot_date=DATE, run_id=RUN,
        run_info=m.new_run_info(S, "manual", "full", T0, T0), outcomes=outcomes,
        requested_regions=["us-east-1", "eu-west-1"], created_at=T0,
    )
    m.write_revision(store, man)


def test_list_raw_rows_with_purge_dates(store):
    _seed(store)
    rows = raw_access.list_raw(store, "aws", S.raw_retention_days)
    assert [(r.snapshot_date, r.region, r.run_id) for r in rows] == [
        (DATE, "eu-west-1", RUN),
        (DATE, "us-east-1", RUN),
    ]
    assert rows[0].file_count == 2
    assert rows[0].bytes > 0
    assert rows[0].stored_at == "2026-10-05T13:30:00Z"
    assert rows[0].purge_after == "2026-11-05"


def test_list_raw_without_manifest_uses_object_metadata(store):
    key = layout.raw_file_key("aws", "2026-09-28", "us-east-1", "20260928T130000Z-222222", "pricing-X-us-east-1.json")
    store.put_bytes(key, b"zst")
    (row,) = raw_access.list_raw(store, "aws", 30)
    assert row.snapshot_date == "2026-09-28" and row.file_count == 1
    assert row.purge_after  # derived from the object's upload time


def test_download_compressed_and_decompressed(store, tmp_path):
    _seed(store)
    files = raw_access.download_raw(store, "aws", DATE, str(tmp_path / "zst"), regions=["us-east-1"])
    assert sorted(p.split("/")[-1] for p in files) == [
        "pricing-AmazonEC2-us-east-1.json.zst",
        "pricing-AmazonS3-us-east-1.json.zst",
    ]
    files = raw_access.download_raw(store, "aws", DATE, str(tmp_path / "json"), decompress=True)
    assert len(files) == 4
    ec2 = [p for p in files if p.endswith("pricing-AmazonEC2-eu-west-1.json")][0]
    assert open(ec2).read() == '{"svc": "AmazonEC2"}'


def test_download_missing_snapshot_raises(store, tmp_path):
    with pytest.raises(raw_access.RawNotFound):
        raw_access.download_raw(store, "aws", "2020-01-06", str(tmp_path))

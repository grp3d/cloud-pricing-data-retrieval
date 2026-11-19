"""download-latest: copy the snapshot latest.json names, with its metadata, to a local dir."""

import datetime as dt
import hashlib
import json
import os

import pytest
from click.testing import CliRunner

from src.pipeline import latest, latest_download, layout
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from src.pipeline.storage import open_storage

T0 = dt.datetime(2026, 10, 5, 14, 0, 0, tzinfo=dt.timezone.utc)
RUN = "20261005T130000Z-111111"
SETTINGS = PipelineSettings.from_env({})
TABLE = layout.tables_for("aws")[0]


def _publish(store, date, regions=("us-east-1", "eu-west-1"), status="succeeded", revision=1):
    tables = {}
    for table in layout.tables_for("aws"):
        entries = {}
        for region in regions:
            key = layout.parquet_file_key("aws", table, date, region, RUN)
            body = f"{table}/{date}/{region}".encode()
            store.put_bytes(key, body)
            f = m.DataFile(key, len(body), hashlib.sha256(body).hexdigest(), 1)
            entries[region] = m.RegionTableEntry(RUN, 1, [f])
        tables[table] = m.TableEntry(1, len(regions), entries)
    man = m.Manifest(
        manifest_version=m.MANIFEST_VERSION, provider="aws", snapshot_date=date, run_id=RUN,
        revision=revision, previous_revision=revision - 1 or None, created_at="2026-10-05T13:00:00Z",
        origin="pipeline", status=status, requested=list(regions), succeeded=list(regions), failed=[],
        run=m.new_run_info(SETTINGS, "manual", "full", T0, T0), raw={}, tables=tables, purged=None,
    )
    m.write_revision(store, man)
    latest.update_latest(store, man, T0)
    return man


def test_copies_latest_tables_and_metadata(store, tmp_path):
    _publish(store, "2026-09-28")
    man = _publish(store, "2026-10-05")
    dest = tmp_path / "out"
    result = latest_download.download_latest(store, "aws", str(dest))

    assert result.snapshot_date == "2026-10-05" and result.revision == 1
    assert len(result.downloaded) == len(m.active_file_paths(man)) and not result.skipped
    assert not list(dest.rglob("*snapshot_date=2026-09-28*"))  # only the latest date

    local = open_storage(f"file://{dest}")  # the copy is a valid local store
    assert latest.read_latest(local, "aws") == latest.read_latest(store, "aws")
    assert m.read_current(local, "aws", "2026-10-05").to_dict() == man.to_dict()
    assert local.head(layout.revision_key("aws", "2026-10-05", 1)) is not None
    for path in m.active_file_paths(man):
        assert local.get_bytes(path) == store.get_bytes(path)


def test_rerun_skips_files_already_present(store, tmp_path):
    _publish(store, "2026-10-05")
    first = latest_download.download_latest(store, "aws", str(tmp_path))
    second = latest_download.download_latest(store, "aws", str(tmp_path))
    assert not second.downloaded and len(second.skipped) == len(first.downloaded)


def test_no_latest_raises(store, tmp_path):
    with pytest.raises(latest_download.LatestNotFound):
        latest_download.download_latest(store, "aws", str(tmp_path))


def test_checksum_mismatch_raises_and_writes_no_metadata(store, tmp_path):
    man = _publish(store, "2026-10-05")
    store.put_bytes(sorted(m.active_file_paths(man))[0], b"tampered")
    with pytest.raises(latest_download.ChecksumMismatch):
        latest_download.download_latest(store, "aws", str(tmp_path))
    assert not os.path.exists(tmp_path / layout.latest_key("aws"))
    assert not list(tmp_path.rglob("*.part"))


def test_cli(local_store, tmp_path, monkeypatch):
    from src.pipeline.__main__ import cli

    monkeypatch.setenv("PIPELINE_STORAGE_URI", local_store.uri)
    monkeypatch.setenv("PIPELINE_PROVIDER", "aws")
    dest = tmp_path / "cli-out"
    runner = CliRunner()

    missing = runner.invoke(cli, ["download-latest", "--dest", str(dest)])
    assert missing.exit_code == 3

    _publish(local_store, "2026-10-05")
    ok = runner.invoke(cli, ["download-latest", "--dest", str(dest)])
    assert ok.exit_code == 0, ok.output
    summary = json.loads(ok.output.strip().splitlines()[-1])
    assert summary["snapshot_date"] == "2026-10-05" and summary["downloaded"] > 0


def test_dest_accepts_file_uri_and_rejects_s3(store, tmp_path):
    _publish(store, "2026-10-05")
    dest = tmp_path / "uri dest"
    latest_download.download_latest(store, "aws", dest.as_uri())
    assert (dest / layout.latest_key("aws")).exists()
    with pytest.raises(ValueError):
        latest_download.download_latest(store, "aws", "s3://bucket/x")


def test_keeps_newer_local_latest(store, tmp_path):
    _publish(store, "2026-10-05")
    newer = {"snapshot_date": "2026-11-01", "revision": 1}
    path = tmp_path / layout.latest_key("aws")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(newer))
    latest_download.download_latest(store, "aws", str(tmp_path))
    assert json.loads(path.read_text()) == newer
    assert (tmp_path / layout.manifest_key("aws", "2026-10-05")).exists()


def test_logs_each_operation(store, tmp_path, caplog):
    import logging

    man = _publish(store, "2026-10-05")
    with caplog.at_level(logging.INFO, logger="pipeline.download_latest"):
        latest_download.download_latest(store, "aws", str(tmp_path))
    events = [json.loads(r.getMessage())["event"] for r in caplog.records]
    assert events[:3] == ["read_latest", "read_manifest", "download_start"]
    assert events.count("download") == len(m.active_file_paths(man))
    assert events.count("write_metadata") == 3

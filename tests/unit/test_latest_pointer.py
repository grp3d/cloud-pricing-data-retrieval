"""
latest.json compare-and-swap (FR-012, FR-014, research R7). Test-first per constitution IV.
"""

import datetime as dt
import json

import pytest

from src.pipeline import latest, layout
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from src.pipeline.storage import PreconditionFailed

T0 = dt.datetime(2026, 10, 5, 14, 0, 0, tzinfo=dt.timezone.utc)
SETTINGS = PipelineSettings.from_env({})


def _manifest(date, revision=1, status="succeeded", run_id="20261005T130000Z-111111"):
    region_entry = m.RegionTableEntry(written_by_run=run_id, row_count=0, files=[])
    return m.Manifest(
        manifest_version=m.MANIFEST_VERSION,
        provider="aws",
        snapshot_date=date,
        run_id=run_id,
        revision=revision,
        previous_revision=revision - 1 or None,
        created_at="2026-10-05T13:00:00Z",
        origin="pipeline",
        status=status,
        requested=["us-east-1"],
        succeeded=["us-east-1"] if status == "succeeded" else [],
        failed=[],
        run=m.new_run_info(SETTINGS, "manual", "full", T0, T0),
        raw={},
        tables={t: m.TableEntry(1, 0, {"us-east-1": region_entry}) for t in m.TABLES}
        if status == "succeeded"
        else {},
        purged=None,
    )


def _pointer(store):
    return json.loads(store.get_bytes(layout.latest_key("aws")))


def test_creates_latest_when_missing(store):
    assert latest.update_latest(store, _manifest("2026-10-05"), T0) is True
    doc = _pointer(store)
    assert doc == {
        "manifest_version": "1.0",
        "provider": "aws",
        "snapshot_date": "2026-10-05",
        "revision": 1,
        "run_id": "20261005T130000Z-111111",
        "manifest_path": "aws/manifests/2026-10-05/manifest.json",
        "updated_at": "2026-10-05T14:00:00Z",
    }
    assert latest.read_latest(store, "aws")["snapshot_date"] == "2026-10-05"


@pytest.mark.parametrize("status", ["partial", "failed", "purged"])
def test_only_succeeded_moves_latest(store, status):
    assert latest.update_latest(store, _manifest("2026-10-05", status=status), T0) is False
    assert store.head(layout.latest_key("aws")) is None


def test_never_moves_backwards(store):
    latest.update_latest(store, _manifest("2026-10-12"), T0)
    assert latest.update_latest(store, _manifest("2026-10-05", revision=5), T0) is False
    assert _pointer(store)["snapshot_date"] == "2026-10-12"


def test_same_date_moves_only_to_higher_revision(store):
    latest.update_latest(store, _manifest("2026-10-05", revision=2), T0)
    assert latest.update_latest(store, _manifest("2026-10-05", revision=1), T0) is False
    assert latest.update_latest(store, _manifest("2026-10-05", revision=3), T0) is True
    assert _pointer(store)["revision"] == 3


def test_newer_date_moves_forward(store):
    latest.update_latest(store, _manifest("2026-10-05", revision=4), T0)
    assert latest.update_latest(store, _manifest("2026-10-12"), T0) is True
    assert _pointer(store)["snapshot_date"] == "2026-10-12"


def test_retries_on_concurrent_writer_then_succeeds(store, monkeypatch):
    latest.update_latest(store, _manifest("2026-10-05"), T0)
    real = store.put_if_match
    calls = {"n": 0}

    def flaky(key, data, etag):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PreconditionFailed(key)
        return real(key, data, etag)

    monkeypatch.setattr(store, "put_if_match", flaky)
    assert latest.update_latest(store, _manifest("2026-10-12"), T0) is True
    assert calls["n"] == 3


def test_gives_up_after_bounded_retries(store, monkeypatch):
    latest.update_latest(store, _manifest("2026-10-05"), T0)

    def always_conflict(key, data, etag):
        raise PreconditionFailed(key)

    monkeypatch.setattr(store, "put_if_match", always_conflict)
    with pytest.raises(latest.LatestUpdateConflict):
        latest.update_latest(store, _manifest("2026-10-12"), T0)

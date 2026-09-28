"""
Manifest revisions (data-model.md, research R6; FR-013–FR-017, FR-043).

Test-first per constitution IV: status derivation, carry-forward and revision rules
decide what consumers see, so they are pinned here before implementation.
"""

import datetime as dt
import json

import pytest

from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings

DATE = "2026-10-05"
RUN1 = "20261005T130000Z-111111"
RUN2 = "20261005T140000Z-222222"
T0 = dt.datetime(2026, 10, 5, 13, 0, 0, tzinfo=dt.timezone.utc)
SETTINGS = PipelineSettings.from_env({"PIPELINE_HOST_LABEL": "home-server", "GIT_SHA": "abc1234"})


def _tables_for(region, run_id, rows=10):
    return {
        table: m.RegionTableEntry(
            written_by_run=run_id,
            row_count=rows,
            files=[
                m.DataFile(
                    path=f"aws/parquet/{table}/snapshot_date={DATE}/region={region}/part-{run_id}.parquet",
                    bytes=100,
                    sha256="0" * 64,
                    row_count=rows,
                )
            ],
        )
        for table in m.TABLES
    }


def ok(region, run_id=RUN1, rows=10):
    return m.RegionOutcome(
        region=region,
        succeeded=True,
        attempts=1,
        services_downloaded=2,
        services_without_price_list=1,
        unparseable_prices=0,
        tables=_tables_for(region, run_id, rows),
        raw=m.make_raw_entry(
            f"aws/raw/{DATE}/{region}/{run_id}/", 2, 500, T0, SETTINGS.raw_retention_days
        ),
    )


def failed(region, reason="HTTP 403", attempts=1):
    return m.RegionOutcome(region=region, succeeded=False, attempts=attempts, reason=reason)


def build(previous, outcomes, requested, run_id=RUN1, mode="full"):
    run_info = m.new_run_info(SETTINGS, trigger="manual", mode=mode, started_at=T0, ended_at=T0)
    return m.build_revision(
        previous,
        provider="aws",
        snapshot_date=DATE,
        run_id=run_id,
        run_info=run_info,
        outcomes=outcomes,
        requested_regions=requested,
        created_at=T0,
    )


def test_revision_one_succeeded():
    man = build(None, [ok("us-east-1"), ok("eu-west-1")], ["us-east-1", "eu-west-1"])
    assert man.revision == 1 and man.previous_revision is None
    assert man.status == "succeeded"
    assert man.requested == ["eu-west-1", "us-east-1"]
    assert man.succeeded == ["eu-west-1", "us-east-1"]
    assert man.failed == []
    assert set(man.tables) == set(m.TABLES)
    assert man.tables["price_fact"].row_count == 20
    assert man.tables["price_fact"].schema_version == 1
    assert man.origin == "pipeline"
    assert man.manifest_version == m.MANIFEST_VERSION == "1.0"


def test_partial_and_failed_status():
    partial = build(None, [ok("us-east-1"), failed("eu-west-1")], ["us-east-1", "eu-west-1"])
    assert partial.status == "partial"
    assert partial.failed == [m.FailedRegion("eu-west-1", "HTTP 403", 1)]
    assert set(partial.tables["price_fact"].regions) == {"us-east-1"}

    none = build(None, [failed("us-east-1"), failed("eu-west-1")], ["us-east-1", "eu-west-1"])
    assert none.status == "failed"
    assert none.tables == {}
    assert none.raw == {}


def test_carry_forward_replaces_only_regions_that_succeeded_now():
    rev1 = build(None, [ok("us-east-1"), failed("eu-west-1")], ["us-east-1", "eu-west-1"])
    rev2 = build(rev1, [ok("eu-west-1", RUN2)], ["eu-west-1"], run_id=RUN2, mode="regions")
    assert rev2.revision == 2 and rev2.previous_revision == 1
    assert rev2.status == "succeeded"
    assert rev2.requested == ["eu-west-1", "us-east-1"]
    regions = rev2.tables["price_fact"].regions
    assert regions["us-east-1"].written_by_run == RUN1  # carried forward unchanged
    assert regions["eu-west-1"].written_by_run == RUN2
    assert rev2.raw["us-east-1"].location.endswith(f"/{RUN1}/")
    assert rev2.run.region_results[0].region == "eu-west-1"


def test_failed_rerun_keeps_previous_good_data_and_status_never_regresses():
    rev1 = build(None, [ok("us-east-1")], ["us-east-1"])
    rev2 = build(rev1, [failed("us-east-1", "ReadTimeout", 4)], ["us-east-1"], run_id=RUN2)
    assert rev2.status == "succeeded"
    assert rev2.tables["price_fact"].regions["us-east-1"].written_by_run == RUN1
    assert rev2.run.region_results[0].outcome == "failed"
    assert rev2.run.region_results[0].attempts == 4


def test_requested_is_union_of_previous_and_current():
    rev1 = build(None, [ok("us-east-1")], ["us-east-1"])
    rev2 = build(rev1, [failed("ap-south-1")], ["ap-south-1"], run_id=RUN2)
    assert rev2.requested == ["ap-south-1", "us-east-1"]
    assert rev2.status == "partial"


def test_purged_previous_is_not_carried_forward():
    rev1 = build(None, [ok("us-east-1"), ok("eu-west-1")], ["us-east-1", "eu-west-1"])
    purged = m.build_purged_revision(rev1, T0)
    rev3 = build(purged, [ok("us-east-1", RUN2)], ["us-east-1"], run_id=RUN2)
    assert rev3.revision == 3
    assert rev3.requested == ["us-east-1"]
    assert rev3.status == "succeeded"


def test_failure_reason_truncated_to_500_chars():
    man = build(None, [failed("us-east-1", "x" * 900)], ["us-east-1"])
    assert len(man.failed[0].reason) == 500
    assert len(man.run.region_results[0].reason) == 500


def test_raw_purge_after():
    entry = m.make_raw_entry("aws/raw/x/", 1, 10, dt.datetime(2026, 10, 5, 23, 59, tzinfo=dt.timezone.utc), 30)
    assert entry.stored_at == "2026-10-05T23:59:00Z"
    assert entry.purge_after == "2026-11-05"


def test_run_info_from_settings():
    man = build(None, [ok("us-east-1")], ["us-east-1"])
    assert man.run.host == "home-server"
    assert man.run.pipeline_version == {"git_sha": "abc1234", "image_tag": "local"}
    assert man.run.region_results[0].services_without_price_list == 1
    assert man.run.region_results[0].unparseable_prices == 0


def test_json_roundtrip_is_deterministic():
    man = build(None, [ok("us-east-1"), failed("eu-west-1")], ["us-east-1", "eu-west-1"])
    text = man.to_json()
    assert text == m.Manifest.from_json(text).to_json()
    doc = json.loads(text)
    assert doc["created_at"] == "2026-10-05T13:00:00Z"
    assert doc["regions"]["failed"] == [{"region": "eu-west-1", "reason": "HTTP 403", "attempts": 1}]
    assert list(doc) == sorted(doc)


def test_active_file_paths():
    man = build(None, [ok("us-east-1")], ["us-east-1"])
    paths = m.active_file_paths(man)
    assert len(paths) == len(m.TABLES)
    assert all(p.startswith("aws/parquet/") for p in paths)
    assert m.active_file_paths(m.build_purged_revision(man, T0)) == set()


def test_write_and_read_revisions(store):
    rev1 = build(None, [ok("us-east-1")], ["us-east-1"])
    m.write_revision(store, rev1)
    assert store.get_bytes("aws/manifests/2026-10-05/revisions/0001.json") == store.get_bytes(
        "aws/manifests/2026-10-05/manifest.json"
    )
    current = m.read_current(store, "aws", DATE)
    assert current.to_json() == rev1.to_json()
    assert m.next_revision_number(store, "aws", DATE) == 2
    assert m.read_current(store, "aws", "2026-10-12") is None
    assert m.next_revision_number(store, "aws", "2026-10-12") == 1


def test_write_revision_never_overwrites_history(store):
    rev1 = build(None, [ok("us-east-1")], ["us-east-1"])
    m.write_revision(store, rev1)
    with pytest.raises(m.RevisionExists):
        m.write_revision(store, rev1)

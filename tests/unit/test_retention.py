"""
Retention decisions (FR-020–FR-024, FR-045, FR-046, FR-048–FR-050; research R10).

Test-first per constitution IV: this code decides what gets deleted, and price history
cannot be downloaded again.
"""

import datetime as dt
import os

import pytest

from src.pipeline import claims, layout, retention
from src.pipeline import manifest as m
from src.pipeline.config import PipelineSettings
from src.pipeline.latest import update_latest

NOW = dt.datetime(2026, 10, 5, 13, 0, 0, tzinfo=dt.timezone.utc)
RETENTION_RUN = "20261005T130000Z-feed00"


def settings(**env):
    return PipelineSettings.from_env({"PRICING_REGIONS": "us-east-1,eu-west-1", **env})


def _run_id(date: str, n: int = 0) -> str:
    return f"{date.replace('-', '')}T13000{n}Z-{n:06x}"


def seed(store, date, status="succeeded", *, run_n=0, created_at=None, previous=None, move_latest=True):
    """Write one revision for `date` whose data files really exist in the store."""
    run_id = _run_id(date, run_n)
    created = created_at or dt.datetime.fromisoformat(date).replace(hour=14, tzinfo=dt.timezone.utc)
    outcomes = []
    for region in ("us-east-1", "eu-west-1"):
        ok = status == "succeeded" or (status == "partial" and region == "us-east-1")
        if not ok:
            outcomes.append(m.RegionOutcome(region, False, 1, reason="HTTP 403"))
            continue
        tables = {}
        for table in layout.TABLES:
            key = layout.parquet_file_key("aws", table, date, region, run_id)
            store.put_bytes(key, b"parquet")
            tables[table] = m.RegionTableEntry(run_id, 1, [m.DataFile(key, 7, "0" * 64, 1)])
        outcomes.append(m.RegionOutcome(region, True, 1, tables=tables, raw=None))
    man = m.build_revision(
        previous if previous is not None else m.read_current(store, "aws", date),
        provider="aws",
        snapshot_date=date,
        run_id=run_id,
        run_info=m.new_run_info(settings(), "scheduled", "full", created, created),
        outcomes=outcomes,
        requested_regions=["us-east-1", "eu-west-1"],
        created_at=created,
        revision=m.next_revision_number(store, "aws", date),
    )
    m.write_revision(store, man)
    if move_latest:
        update_latest(store, man, created)
    return man


def parquet_keys(store, date):
    return sorted(
        o.key for t in layout.TABLES for o in store.list(layout.parquet_date_prefix("aws", t, date))
    )


def mondays(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d.isoformat()
        d += dt.timedelta(days=7)


def run(store, clock=None, sleep=None, **kw):
    return retention.run_retention(
        store,
        settings(**kw.pop("env", {})),
        now=(clock or (lambda: NOW))(),
        sleep=sleep or (lambda s: None),
        run_id=RETENTION_RUN,
        **kw,
    )


# --- thinning -------------------------------------------------------------------------


@pytest.fixture
def history(store):
    """Weekly Mondays from 2025-07-07 to 2026-09-28 (15 months), with some bad weeks."""
    special = {"2025-08-04": "partial", "2025-09-01": "failed", "2025-09-08": "failed",
               "2025-09-15": "partial", "2025-09-22": "failed", "2025-09-29": "failed"}
    for date in mondays(dt.date(2025, 7, 7), dt.date(2026, 9, 28)):
        seed(store, date, special.get(date, "succeeded"))
    return store


def test_thinning_keeps_earliest_succeeded_per_old_month(history):
    result = run(history)
    kept = {k["month"]: k["snapshot_date"] for k in result["keep_monthly"]}
    assert kept["2025-07"] == "2025-07-07"
    assert kept["2025-08"] == "2025-08-11"  # 08-04 is partial → next succeeded kept
    assert "2025-09" not in kept  # no succeeded snapshot in September
    purged = {p["snapshot_date"] for p in result["purge_snapshots"]}
    assert "2025-08-04" in purged and "2025-08-18" in purged and "2025-08-25" in purged
    assert {"2025-09-01", "2025-09-08", "2025-09-15", "2025-09-22", "2025-09-29"} <= purged
    assert "2025-07-07" not in purged and "2025-08-11" not in purged
    # Within the 12-month window nothing is thinned (cutoff 2025-10-05).
    assert not [d for d in purged if d >= "2025-10-05"]
    for date in purged:
        assert m.read_current(history, "aws", date).status == "purged"
        assert parquet_keys(history, date) == []
    assert parquet_keys(history, "2025-08-11")


def test_latest_is_never_purged(store):
    only = seed(store, "2025-03-10")  # very old, but it's what latest.json names
    seed(store, "2025-03-17", move_latest=False)
    result = run(store)
    purged = {p["snapshot_date"] for p in result["purge_snapshots"]}
    assert "2025-03-10" not in purged
    assert m.read_current(store, "aws", "2025-03-10").status == only.status
    assert "2025-03-17" in purged


def test_purge_writes_manifest_before_deleting(store, monkeypatch):
    seed(store, "2025-03-03")
    seed(store, "2025-03-10")
    seed(store, "2026-10-05")
    real_delete = store.delete
    seen = []

    def checked_delete(key):
        if "/snapshot_date=2025-03-10/" in key:
            seen.append(m.read_current(store, "aws", "2025-03-10").status)
        real_delete(key)

    monkeypatch.setattr(store, "delete", checked_delete)
    run(store)
    assert seen and set(seen) == {"purged"}
    purged = m.read_current(store, "aws", "2025-03-10")
    assert purged.revision == 2 and purged.tables == {} and purged.purged["reason"] == "retention-thinning"


# --- superseded files and orphans -------------------------------------------------------


def test_superseded_files_wait_for_grace_period(store):
    date = "2026-10-05"
    rev1 = seed(store, date)
    rev2 = seed(store, date, run_n=1, created_at=NOW - dt.timedelta(minutes=2))
    old = m.active_file_paths(rev1)

    early = run(store)  # other date (no own_date): no waiting
    assert all(item["action"] == "keep" for item in early["superseded"])
    assert set(parquet_keys(store, date)) >= old

    later = run(store, clock=lambda: NOW + dt.timedelta(minutes=4))
    assert {i["path"] for i in later["superseded"] if i["action"] == "delete"} == old
    assert set(parquet_keys(store, date)) == m.active_file_paths(rev2)


def test_orphans_are_deleted_after_grace(store):
    """Orphans (never listed by any revision) age from their own upload time."""
    real_now = dt.datetime.now(dt.timezone.utc)
    date = real_now.date().isoformat()
    seed(store, date, created_at=real_now - dt.timedelta(hours=1))
    orphan = layout.parquet_file_key("aws", "price_fact", date, "us-east-1", _run_id(date, 9))
    store.put_bytes(orphan, b"half-written")

    fresh = run(store, clock=lambda: real_now + dt.timedelta(minutes=1))
    assert [(o["path"], o["action"]) for o in fresh["orphans"]] == [(orphan, "keep")]
    assert store.head(orphan) is not None

    aged = run(store, clock=lambda: real_now + dt.timedelta(minutes=10))
    assert [o["path"] for o in aged["orphans"]] == [orphan]
    assert store.head(orphan) is None


def test_guard_skips_file_referenced_again_before_delete(store):
    date = "2026-10-05"
    rev1 = seed(store, date)
    seed(store, date, run_n=1, created_at=NOW - dt.timedelta(hours=1))
    s = settings()
    plan = retention.plan_retention(store, s, NOW, run_id=RETENTION_RUN)
    doomed = {i["path"] for i in plan["superseded"] if i["action"] == "delete"}
    assert doomed == m.active_file_paths(rev1)

    # A concurrent revision lists the old files again between planning and deleting.
    again = m.build_revision(
        m.read_current(store, "aws", date), provider="aws", snapshot_date=date, run_id=_run_id(date, 2),
        run_info=m.new_run_info(s, "manual", "regions", NOW, NOW),
        outcomes=[m.RegionOutcome("us-east-1", True, 1, tables={
            t: rev1.tables[t].regions["us-east-1"] for t in layout.TABLES})],
        requested_regions=["us-east-1"], created_at=NOW, revision=3,
    )
    m.write_revision(store, again)

    result = retention.apply_retention(store, s, plan, NOW, sleep=lambda x: None, run_id=RETENTION_RUN)
    reprotected = {p for p in doomed if "region=us-east-1" in p}
    assert reprotected <= set(result["skipped_guard"])
    for path in reprotected:
        assert store.head(path) is not None


def test_busy_date_is_skipped(store):
    date = "2026-10-05"
    rev1 = seed(store, date)
    seed(store, date, run_n=1, created_at=NOW - dt.timedelta(hours=1))
    claims.acquire(store, "aws", date, "20261005T120000Z-aaaaaa", "manual", 180, NOW)
    result = run(store)
    assert date in result["skipped_busy_dates"]
    assert set(parquet_keys(store, date)) >= m.active_file_paths(rev1)


def test_dry_run_changes_nothing_and_matches_real_run(history):
    date = "2026-09-28"
    seed(history, date, run_n=1, created_at=NOW - dt.timedelta(hours=1))
    before = sorted(o.key for o in history.list("aws/"))
    plan = run(history, dry_run=True)
    assert sorted(o.key for o in history.list("aws/")) == before
    real = run(history)

    def planned(p):
        return (
            {i["path"] for i in p["superseded"] + p["orphans"] if i["action"] != "keep"},
            {x["snapshot_date"] for x in p["purge_snapshots"]},
        )

    assert planned(plan) == planned(real)
    assert set(real["deleted"]) >= planned(plan)[0]


# --- inline wait (own date) ------------------------------------------------------------


def test_inline_wait_for_remaining_grace(store, no_sleep):
    date = "2026-10-05"
    rev1 = seed(store, date)
    seed(store, date, run_n=1, created_at=NOW - dt.timedelta(minutes=2))
    result = run(store, sleep=no_sleep, own_date=date)
    assert no_sleep.calls == [pytest.approx(180)]
    assert result["waited_seconds"] == pytest.approx(180)
    for path in m.active_file_paths(rev1):
        assert store.head(path) is None


def test_no_inline_wait_when_grace_exceeds_limit(store, no_sleep):
    date = "2026-10-05"
    rev1 = seed(store, date)
    seed(store, date, run_n=1, created_at=NOW - dt.timedelta(minutes=2))
    result = run(store, sleep=no_sleep, own_date=date, env={"SUPERSEDED_FILE_GRACE_MINUTES": "10"})
    assert no_sleep.calls == []
    assert result["waited_seconds"] == 0
    for path in m.active_file_paths(rev1):
        assert store.head(path) is not None


# --- local raw expiry -------------------------------------------------------------------


def test_local_raw_expiry(local_store):
    old = layout.raw_file_key("aws", "2026-08-01", "us-east-1", _run_id("2026-08-01"), "pricing-AmazonS3-us-east-1.json")
    new = layout.raw_file_key("aws", "2026-10-01", "us-east-1", _run_id("2026-10-01"), "pricing-AmazonS3-us-east-1.json")
    for key in (old, new):
        local_store.put_bytes(key, b"zst")
    stamp = (NOW - dt.timedelta(days=40)).timestamp()
    os.utime(os.path.join(local_store.root, *old.split("/")), (stamp, stamp))
    result = run(local_store)
    assert result["raw_local"] == [old]
    assert local_store.head(old) is None
    assert local_store.head(new) is not None


def test_s3_raw_left_to_lifecycle_rules(s3_store):
    key = layout.raw_file_key("aws", "2020-01-06", "us-east-1", _run_id("2020-01-06"), "pricing-AmazonS3-us-east-1.json")
    s3_store.put_bytes(key, b"zst")
    result = run(s3_store)
    assert result["raw_local"] == []
    assert s3_store.head(key) is not None


def test_local_raw_of_busy_date_is_kept(local_store):
    """A transform-only run holding a date's claim may be reading that date's raw files;
    another run's retention must not delete them (PR review finding)."""
    busy, idle = "2026-08-01", "2026-08-08"
    keys = {}
    for date in (busy, idle):
        keys[date] = layout.raw_file_key(
            "aws", date, "us-east-1", _run_id(date), "pricing-AmazonS3-us-east-1.json"
        )
        local_store.put_bytes(keys[date], b"zst")
        stamp = (NOW - dt.timedelta(days=40)).timestamp()
        os.utime(os.path.join(local_store.root, *keys[date].split("/")), (stamp, stamp))
    claims.acquire(local_store, "aws", busy, "20261005T120000Z-aaaaaa", "manual", 180, NOW)

    plan = run(local_store, dry_run=True)
    assert plan["raw_local"] == [keys[idle]]
    assert busy in plan["skipped_busy_dates"]

    result = run(local_store)
    assert local_store.head(keys[busy]) is not None
    assert local_store.head(keys[idle]) is None
    assert busy in result["skipped_busy_dates"]


def test_local_raw_deletion_respects_a_claim_taken_after_planning(local_store):
    """Raw files are deleted inside the per-date claim, so a run that starts between planning
    and applying still protects its raw input."""
    date = "2026-08-01"
    key = layout.raw_file_key("aws", date, "us-east-1", _run_id(date), "pricing-AmazonS3-us-east-1.json")
    local_store.put_bytes(key, b"zst")
    stamp = (NOW - dt.timedelta(days=40)).timestamp()
    os.utime(os.path.join(local_store.root, *key.split("/")), (stamp, stamp))
    s = settings()
    plan = retention.plan_retention(local_store, s, NOW, run_id=RETENTION_RUN)
    assert plan["raw_local"] == [key]

    claims.acquire(local_store, "aws", date, "20261005T120000Z-bbbbbb", "manual", 180, NOW)
    result = retention.apply_retention(local_store, s, plan, NOW, sleep=lambda x: None, run_id=RETENTION_RUN)
    assert local_store.head(key) is not None
    assert date in result["skipped_busy_dates"]
    assert result["raw_deleted"] == [] and retention.summarize(result)["raw_deleted_local"] == 0

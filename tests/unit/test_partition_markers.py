"""
Tests for src/partition_markers.py — _SUCCESS / _REGION_COMPLETE markers and
the cross-process partition lock.

Multiprocess tests use the "spawn" context so each worker opens its own lock
file descriptor, exercising real flock contention between processes.
"""

import multiprocessing
import os
import time

import pytest

from src.partition_markers import (
    LOCK_DIR_NAME,
    REGION_COMPLETE_MARKER,
    SUCCESS_MARKER,
    FinalizeStatus,
    _atomic_touch,
    finalize_partition,
    invalidate_region,
    mark_region_complete,
    partition_lock,
)

SNAP = "2026-09-27"
REGIONS = ["us-east-1", "eu-west-1", "ap-northeast-1"]


def _region_path(root, table, region, snap=SNAP):
    return root / table / f"snapshot_date={snap}" / f"region={region}"


def _write_region(root, table, region, with_data=True, complete=True, snap=SNAP):
    region_dir = _region_path(root, table, region, snap)
    region_dir.mkdir(parents=True, exist_ok=True)
    if with_data:
        (region_dir / "part-0.parquet").write_bytes(b"data")
    if complete:
        (region_dir / REGION_COMPLETE_MARKER).touch()
    return region_dir


def _hold_lock(parquet_root, table, snapshot_date, acquired, release, hold_seconds):
    with partition_lock(parquet_root, table, snapshot_date):
        acquired.set()
        release.wait(hold_seconds)


def _time_lock_acquisition(parquet_root, table, snapshot_date, result_queue):
    with partition_lock(parquet_root, table, snapshot_date):
        result_queue.put(time.monotonic())


def _stress_worker(parquet_root, region, expected_regions, iterations):
    for _ in range(iterations):
        invalidate_region(parquet_root, "price_fact", SNAP, region)
        region_dir = os.path.join(
            parquet_root, "price_fact", f"snapshot_date={SNAP}", f"region={region}"
        )
        os.makedirs(region_dir, exist_ok=True)
        with open(os.path.join(region_dir, "part-0.parquet"), "wb") as fh:
            fh.write(b"data")
        mark_region_complete(parquet_root, "price_fact", SNAP, region)
        finalize_partition(parquet_root, "price_fact", SNAP, expected_regions)


def _stress_checker(parquet_root, expected_regions, stop, violations, checks):
    partition = os.path.join(parquet_root, "price_fact", f"snapshot_date={SNAP}")
    count = 0
    while not stop.is_set():
        with partition_lock(parquet_root, "price_fact", SNAP):
            has_success = os.path.exists(os.path.join(partition, SUCCESS_MARKER))
            present = [
                region
                for region in expected_regions
                if os.path.exists(
                    os.path.join(partition, f"region={region}", REGION_COMPLETE_MARKER)
                )
            ]
        count += 1
        if has_success and len(present) != len(expected_regions):
            violations.put(present)
    checks.put(count)


# --- Foundational ---------------------------------------------------------


def test_atomic_touch_creates_empty_file_without_leftovers(tmp_path):
    target = tmp_path / "a" / "b" / "_SUCCESS"
    _atomic_touch(str(target))

    assert target.is_file()
    assert target.stat().st_size == 0
    assert sorted(os.listdir(target.parent)) == ["_SUCCESS"]


def test_partition_lock_file_lives_under_locks_dir_only(tmp_path):
    root = str(tmp_path)
    with partition_lock(root, "price_fact", SNAP):
        pass

    assert (tmp_path / LOCK_DIR_NAME / "price_fact" / f"snapshot_date={SNAP}.lock").is_file()
    assert not (tmp_path / "price_fact").exists()


def test_partition_lock_blocks_second_process_until_released(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    root = str(tmp_path)
    acquired, release = ctx.Event(), ctx.Event()
    results = ctx.Queue()

    holder = ctx.Process(
        target=_hold_lock, args=(root, "price_fact", SNAP, acquired, release, 10)
    )
    holder.start()
    assert acquired.wait(10)

    waiter = ctx.Process(
        target=_time_lock_acquisition, args=(root, "price_fact", SNAP, results)
    )
    waiter.start()
    time.sleep(0.5)
    assert results.empty(), "second process acquired the lock while it was held"

    released_at = time.monotonic()
    release.set()
    acquired_at = results.get(timeout=10)
    holder.join(10)
    waiter.join(10)

    assert acquired_at >= released_at


def test_locks_for_different_tables_or_dates_are_independent(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    root = str(tmp_path)
    acquired, release = ctx.Event(), ctx.Event()
    results = ctx.Queue()

    holder = ctx.Process(
        target=_hold_lock, args=(root, "price_fact", SNAP, acquired, release, 10)
    )
    holder.start()
    assert acquired.wait(10)
    try:
        for table, snap in [("product_dim", SNAP), ("price_fact", "2026-09-20")]:
            other = ctx.Process(
                target=_time_lock_acquisition, args=(root, table, snap, results)
            )
            other.start()
            results.get(timeout=5)
            other.join(5)
    finally:
        release.set()
        holder.join(10)


# --- User Story 1: completion check ---------------------------------------


def test_finalize_incomplete_lists_missing_regions_in_expected_order(tmp_path):
    _write_region(tmp_path, "price_fact", "eu-west-1")

    outcome = finalize_partition(str(tmp_path), "price_fact", SNAP, REGIONS)

    assert outcome.status is FinalizeStatus.INCOMPLETE
    assert outcome.missing_regions == ["us-east-1", "ap-northeast-1"]
    assert not (tmp_path / "price_fact" / f"snapshot_date={SNAP}" / SUCCESS_MARKER).exists()


def test_finalize_writes_empty_success_at_snapshot_level_only(tmp_path):
    for region in REGIONS:
        _write_region(tmp_path, "price_fact", region)

    outcome = finalize_partition(str(tmp_path), "price_fact", SNAP, REGIONS)

    assert outcome.status is FinalizeStatus.WRITTEN
    marker = tmp_path / "price_fact" / f"snapshot_date={SNAP}" / SUCCESS_MARKER
    assert marker.is_file() and marker.stat().st_size == 0
    assert not (tmp_path / "price_fact" / SUCCESS_MARKER).exists()
    for region in REGIONS:
        assert not (_region_path(tmp_path, "price_fact", region) / SUCCESS_MARKER).exists()


def test_finalize_no_data_when_every_region_is_empty(tmp_path):
    for region in REGIONS:
        _write_region(tmp_path, "price_fact", region, with_data=False)

    outcome = finalize_partition(str(tmp_path), "price_fact", SNAP, REGIONS)

    assert outcome.status is FinalizeStatus.NO_DATA
    assert not (tmp_path / "price_fact" / f"snapshot_date={SNAP}" / SUCCESS_MARKER).exists()


def test_finalize_treats_data_without_region_marker_as_missing(tmp_path):
    _write_region(tmp_path, "price_fact", "us-east-1")
    _write_region(tmp_path, "price_fact", "eu-west-1")
    _write_region(tmp_path, "price_fact", "ap-northeast-1", complete=False)

    outcome = finalize_partition(str(tmp_path), "price_fact", SNAP, REGIONS)

    assert outcome.status is FinalizeStatus.INCOMPLETE
    assert outcome.missing_regions == ["ap-northeast-1"]


def test_finalize_ignores_regions_that_are_not_expected(tmp_path):
    for region in REGIONS:
        _write_region(tmp_path, "price_fact", region)
    _write_region(tmp_path, "price_fact", "sa-east-1", complete=False)

    outcome = finalize_partition(str(tmp_path), "price_fact", SNAP, REGIONS)

    assert outcome.status is FinalizeStatus.WRITTEN


def test_finalize_is_idempotent(tmp_path):
    for region in REGIONS:
        _write_region(tmp_path, "price_fact", region)

    first = finalize_partition(str(tmp_path), "price_fact", SNAP, REGIONS)
    second = finalize_partition(str(tmp_path), "price_fact", SNAP, REGIONS)

    assert first.status is second.status is FinalizeStatus.WRITTEN


def test_mark_region_complete_creates_folder_for_empty_region(tmp_path):
    path = mark_region_complete(str(tmp_path), "price_fact", SNAP, "eu-west-1")

    region_dir = _region_path(tmp_path, "price_fact", "eu-west-1")
    assert path == str(region_dir / REGION_COMPLETE_MARKER)
    assert sorted(os.listdir(region_dir)) == [REGION_COMPLETE_MARKER]


# --- User Story 2: date guard ---------------------------------------------


@pytest.mark.parametrize("bad_date", ["", "2026-9-1", "../x", "2026-09-27/..", "2026-09-27\n"])
def test_public_functions_reject_malformed_snapshot_dates(tmp_path, bad_date):
    root = str(tmp_path)
    with pytest.raises(ValueError):
        mark_region_complete(root, "price_fact", bad_date, "us-east-1")
    with pytest.raises(ValueError):
        finalize_partition(root, "price_fact", bad_date, REGIONS)
    with pytest.raises(ValueError):
        with partition_lock(root, "price_fact", bad_date):
            pass
    assert os.listdir(root) == []


# --- User Story 3: invalidation and concurrency ----------------------------


def test_invalidate_region_removes_success_marker_and_data(tmp_path):
    for region in REGIONS:
        _write_region(tmp_path, "price_fact", region)
    partition = tmp_path / "price_fact" / f"snapshot_date={SNAP}"
    (partition / SUCCESS_MARKER).touch()

    invalidate_region(str(tmp_path), "price_fact", SNAP, "eu-west-1")

    assert not (partition / SUCCESS_MARKER).exists()
    assert os.listdir(_region_path(tmp_path, "price_fact", "eu-west-1")) == []
    for region in ["us-east-1", "ap-northeast-1"]:
        assert sorted(os.listdir(_region_path(tmp_path, "price_fact", region))) == [
            REGION_COMPLETE_MARKER,
            "part-0.parquet",
        ]


def test_invalidate_region_tolerates_missing_files(tmp_path):
    invalidate_region(str(tmp_path), "price_fact", SNAP, "eu-west-1")
    assert not (tmp_path / "price_fact").exists()


def test_invalidate_region_never_touches_other_dates(tmp_path):
    old = "2026-09-20"
    _write_region(tmp_path, "price_fact", "eu-west-1", snap=old)
    (tmp_path / "price_fact" / f"snapshot_date={old}" / SUCCESS_MARKER).touch()

    invalidate_region(str(tmp_path), "price_fact", SNAP, "eu-west-1")

    assert (tmp_path / "price_fact" / f"snapshot_date={old}" / SUCCESS_MARKER).exists()
    assert sorted(os.listdir(_region_path(tmp_path, "price_fact", "eu-west-1", old))) == [
        REGION_COMPLETE_MARKER,
        "part-0.parquet",
    ]


def test_concurrent_regions_never_expose_false_success(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    root = str(tmp_path)
    stop = ctx.Event()
    violations, checks = ctx.Queue(), ctx.Queue()

    checker = ctx.Process(
        target=_stress_checker, args=(root, REGIONS, stop, violations, checks)
    )
    workers = [
        ctx.Process(target=_stress_worker, args=(root, region, REGIONS, 50))
        for region in REGIONS
    ]
    checker.start()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0
    stop.set()
    check_count = checks.get(timeout=10)
    checker.join(10)

    assert check_count > 0
    assert violations.empty(), f"_SUCCESS seen with regions missing: {violations.get()}"
    outcome = finalize_partition(root, "price_fact", SNAP, REGIONS)
    assert outcome.status is FinalizeStatus.WRITTEN
    assert (tmp_path / "price_fact" / f"snapshot_date={SNAP}" / SUCCESS_MARKER).exists()

"""
Tests for region_run_lock (FR-017): only one run at a time writes a given
region and snapshot date, without deadlocking against partition_lock.
"""

import multiprocessing
import time

from src.partition_markers import (
    LOCK_DIR_NAME,
    finalize_partition,
    partition_lock,
    region_run_lock,
)

SNAP = "2026-09-27"


def _hold_region_lock(parquet_root, region, acquired, release):
    with region_run_lock(parquet_root, SNAP, region):
        acquired.set()
        release.wait(10)


def _time_region_lock(parquet_root, region, results):
    with region_run_lock(parquet_root, SNAP, region):
        results.put(time.monotonic())


def _region_then_partition(parquet_root, iterations):
    for _ in range(iterations):
        with region_run_lock(parquet_root, SNAP, "us-east-1"):
            with partition_lock(parquet_root, "price_fact", SNAP):
                pass


def _finalize_repeatedly(parquet_root, iterations):
    for _ in range(iterations):
        finalize_partition(parquet_root, "price_fact", SNAP, ["us-east-1"])


def test_region_run_lock_path(tmp_path):
    with region_run_lock(str(tmp_path), SNAP, "us-east-1"):
        pass
    assert (
        tmp_path / LOCK_DIR_NAME / "_regions" / f"snapshot_date={SNAP}" / "region=us-east-1.lock"
    ).is_file()


def test_same_region_runs_one_after_the_other(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    root = str(tmp_path)
    acquired, release = ctx.Event(), ctx.Event()
    results = ctx.Queue()

    holder = ctx.Process(target=_hold_region_lock, args=(root, "us-east-1", acquired, release))
    holder.start()
    assert acquired.wait(10)
    waiter = ctx.Process(target=_time_region_lock, args=(root, "us-east-1", results))
    waiter.start()
    time.sleep(0.5)
    assert results.empty(), "second run of the same region acquired the lock"

    released_at = time.monotonic()
    release.set()
    assert results.get(timeout=10) >= released_at
    holder.join(10)
    waiter.join(10)


def test_different_regions_do_not_block_each_other(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    root = str(tmp_path)
    acquired, release = ctx.Event(), ctx.Event()
    results = ctx.Queue()

    holder = ctx.Process(target=_hold_region_lock, args=(root, "us-east-1", acquired, release))
    holder.start()
    assert acquired.wait(10)
    try:
        other = ctx.Process(target=_time_region_lock, args=(root, "eu-west-1", results))
        other.start()
        results.get(timeout=5)
        other.join(5)
    finally:
        release.set()
        holder.join(10)


def test_region_lock_then_partition_lock_does_not_deadlock(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    root = str(tmp_path)
    processes = [
        ctx.Process(target=_region_then_partition, args=(root, 200)),
        ctx.Process(target=_finalize_repeatedly, args=(root, 200)),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0, "process did not finish (possible deadlock)"

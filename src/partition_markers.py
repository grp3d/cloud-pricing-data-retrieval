"""
Partition success markers for the pricing Parquet datasets.

Consumer contract: a table's snapshot-date partition is complete and safe to
read once `<parquet_root>/<table>/snapshot_date=<D>/_SUCCESS` exists. It is
written only after every configured region has written that table for D.

Internally, each region records its own completion with an empty
`_REGION_COMPLETE` file inside `<table>/snapshot_date=<D>/region=<R>/`.
Consumers should not depend on it.

Cross-process coordination uses fcntl.flock lock files under
`<parquet_root>/.locks/`, never inside a snapshot-date partition. The lock
files are empty and left in place between runs, which is harmless; deleting
`.locks/` while no run is active is safe. flock is reliable on local
filesystems only, not NFS/SMB.
"""

import fcntl
import glob
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, List, Sequence

SUCCESS_MARKER = "_SUCCESS"
REGION_COMPLETE_MARKER = "_REGION_COMPLETE"
LOCK_DIR_NAME = ".locks"


class FinalizeStatus(str, Enum):
    WRITTEN = "WRITTEN"
    INCOMPLETE = "INCOMPLETE"
    NO_DATA = "NO_DATA"


@dataclass
class FinalizeOutcome:
    status: FinalizeStatus
    missing_regions: List[str] = field(default_factory=list)


_SNAPSHOT_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _validate_snapshot_date(snapshot_date: str) -> None:
    # A malformed date could otherwise resolve to the table root or another
    # partition's folder (FR-003).
    if not isinstance(snapshot_date, str) or not _SNAPSHOT_DATE_RE.fullmatch(snapshot_date):
        raise ValueError(f"Invalid snapshot_date {snapshot_date!r}; expected YYYY-MM-DD")


def _partition_dir(parquet_root: str, table: str, snapshot_date: str) -> str:
    return os.path.join(parquet_root, table, f"snapshot_date={snapshot_date}")


def _region_dir(parquet_root: str, table: str, snapshot_date: str, region: str) -> str:
    return os.path.join(
        _partition_dir(parquet_root, table, snapshot_date), f"region={region}"
    )


def _atomic_touch(path: str) -> None:
    """Create an empty file at `path` that appears atomically (FR-008)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with open(tmp_path, "wb"):
        pass
    os.replace(tmp_path, path)


@contextmanager
def _flock(lock_path: str) -> Iterator[None]:
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing the descriptor releases the flock; the OS also releases it
        # if the process dies, so a crashed run can never leave a stale lock.
        os.close(fd)


@contextmanager
def partition_lock(parquet_root: str, table: str, snapshot_date: str) -> Iterator[None]:
    """Exclusive, blocking, cross-process lock for one (table, snapshot_date)."""
    _validate_snapshot_date(snapshot_date)
    lock_path = os.path.join(
        parquet_root, LOCK_DIR_NAME, table, f"snapshot_date={snapshot_date}.lock"
    )
    with _flock(lock_path):
        yield


@contextmanager
def region_run_lock(parquet_root: str, snapshot_date: str, region: str) -> Iterator[None]:
    """Exclusive, blocking lock for one (snapshot_date, region) (FR-017).

    Held across a region's whole write loop so two runs of the same region and
    date never write at once. Always taken before any partition_lock, and
    partition_lock holders never take it, so the two can't deadlock.
    """
    _validate_snapshot_date(snapshot_date)
    lock_path = os.path.join(
        parquet_root,
        LOCK_DIR_NAME,
        "_regions",
        f"snapshot_date={snapshot_date}",
        f"region={region}.lock",
    )
    with _flock(lock_path):
        yield


def _remove_if_exists(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def invalidate_region(parquet_root: str, table: str, snapshot_date: str, region: str) -> None:
    """Prepare `region` to rewrite `table` for `snapshot_date` (FR-006, FR-013).

    Removes the partition's _SUCCESS, then this region's _REGION_COMPLETE, then
    this region's existing data files, so stale rows can't survive a rewrite
    that now produces none. Other regions and dates are untouched.
    """
    _validate_snapshot_date(snapshot_date)
    with partition_lock(parquet_root, table, snapshot_date):
        region_dir = _region_dir(parquet_root, table, snapshot_date, region)
        _remove_if_exists(
            os.path.join(_partition_dir(parquet_root, table, snapshot_date), SUCCESS_MARKER)
        )
        _remove_if_exists(os.path.join(region_dir, REGION_COMPLETE_MARKER))
        for data_file in glob.glob(os.path.join(region_dir, "*.parquet")):
            _remove_if_exists(data_file)


def mark_region_complete(parquet_root: str, table: str, snapshot_date: str, region: str) -> str:
    """Record that `region` has fully written `table` for `snapshot_date` (FR-012).

    Creates the region folder if needed, so a region with no rows for the
    table still counts as done (FR-015).
    """
    _validate_snapshot_date(snapshot_date)
    path = os.path.join(
        _region_dir(parquet_root, table, snapshot_date, region), REGION_COMPLETE_MARKER
    )
    _atomic_touch(path)
    return path


def finalize_partition(
    parquet_root: str, table: str, snapshot_date: str, expected_regions: Sequence[str]
) -> FinalizeOutcome:
    """Write `_SUCCESS` for (table, snapshot_date) if every expected region is done.

    Complete means every expected region has `_REGION_COMPLETE` (FR-004, FR-014)
    and at least one of them wrote a data file (FR-007). Regions outside
    `expected_regions` are ignored. Safe to call repeatedly.
    """
    _validate_snapshot_date(snapshot_date)
    with partition_lock(parquet_root, table, snapshot_date):
        region_dirs = {
            region: _region_dir(parquet_root, table, snapshot_date, region)
            for region in expected_regions
        }
        missing = [
            region
            for region, region_dir in region_dirs.items()
            if not os.path.isfile(os.path.join(region_dir, REGION_COMPLETE_MARKER))
        ]
        if missing:
            return FinalizeOutcome(FinalizeStatus.INCOMPLETE, missing)

        if not any(
            glob.glob(os.path.join(region_dir, "*.parquet"))
            for region_dir in region_dirs.values()
        ):
            return FinalizeOutcome(FinalizeStatus.NO_DATA)

        _atomic_touch(
            os.path.join(_partition_dir(parquet_root, table, snapshot_date), SUCCESS_MARKER)
        )
        return FinalizeOutcome(FinalizeStatus.WRITTEN)

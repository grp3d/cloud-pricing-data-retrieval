"""
Verify that every file listed by an active manifest exists and matches its recorded
size, sha256 and row count (SC-006).
"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from typing import List, Optional

import pyarrow.parquet as pq

from src.pipeline import manifest as m
from src.pipeline.storage import Storage


@dataclass
class Mismatch:
    snapshot_date: str
    path: str
    problem: str


def verify_snapshot(store: Storage, provider: str, snapshot_date: Optional[str] = None) -> List[Mismatch]:
    dates = [snapshot_date] if snapshot_date else m.list_snapshot_dates(store, provider)
    problems: List[Mismatch] = []
    with tempfile.TemporaryDirectory(prefix="verify-") as tmp:
        for date in dates:
            manifest = m.read_current(store, provider, date)
            if manifest is None:
                problems.append(Mismatch(date, "", "no manifest"))
                continue
            for table in manifest.tables.values() if manifest.status != "purged" else []:
                for region in table.regions.values():
                    for f in region.files:
                        problems.extend(_check_file(store, date, f, tmp))
    return problems


def _check_file(store: Storage, date: str, f: m.DataFile, tmp: str) -> List[Mismatch]:
    info = store.head(f.path)
    if info is None:
        return [Mismatch(date, f.path, "missing")]
    if info.size != f.bytes:
        return [Mismatch(date, f.path, f"size {info.size} != {f.bytes}")]
    local = os.path.join(tmp, "check.parquet")
    store.download_file(f.path, local)
    digest = hashlib.sha256()
    with open(local, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != f.sha256:
        return [Mismatch(date, f.path, "sha256 mismatch")]
    rows = pq.ParquetFile(local).metadata.num_rows
    if rows != f.row_count:
        return [Mismatch(date, f.path, f"row_count {rows} != {f.row_count}")]
    return []

"""
Transform interface the runner uses to turn one location's raw files into Parquet tables
(research R8; constitution v1.3.0 Principle VI).

`LocationTransform` is the base class each provider extends in
`src/<provider>/transformations.py`. The runner calls `load()` on the raw files (once per
location, or once per run for a catalog-wide download), then `transform_location()` for
each location, and `run_counters()` once for the manifest. `TransformResult` is what every
provider returns; `write_table()` writes one staged Parquet file and records its size,
sha256 and row count.
"""

import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass
class TableFile:
    table: str
    region: str
    local_path: str
    bytes: int
    sha256: str
    row_count: int


@dataclass
class TransformResult:
    files: List[TableFile] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    unparseable_prices: int = 0
    # Optional per-location manifest counters: items_received, duplicates_removed, unlocated_rows.
    counters: Dict[str, int] = field(default_factory=dict)
    ok: bool = True

    def failure_reason(self) -> str:
        return "; ".join(self.errors) or "transform produced no tables"


class LocationTransform(ABC):
    # Bump a table's version whenever its columns change incompatibly (FR-011).
    TABLE_SCHEMA_VERSIONS: ClassVar[Dict[str, int]] = {}

    def __init__(self, settings, configured_locations: List[str], log: Optional[Callable[[str], None]] = None):
        self.settings = settings
        self.configured_locations = list(configured_locations)
        self.log = log

    def load(self, raw_files: List[str]) -> Any:
        """Prepare raw files for transforming. The base passes the file list through."""
        return raw_files

    @abstractmethod
    def transform_location(
        self, loaded: Any, location: str, staging_dir: str, run_id: str, snapshot_date: str
    ) -> TransformResult: ...

    def run_counters(self, loaded: Any) -> Dict[str, int]:
        """Run-wide counters for the manifest (e.g. the GCP catalog block)."""
        return {}


def write_table(table: str, location: str, data: pa.Table, staging_dir: str, run_id: str) -> TableFile:
    """Write one table's rows for one location as `part-<run_id>.parquet` in a staging dir."""
    directory = os.path.join(staging_dir, table, f"region={location}")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"part-{run_id}.parquet")
    pq.write_table(data, path, compression="zstd")
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return TableFile(table, location, path, os.path.getsize(path), digest.hexdigest(), data.num_rows)

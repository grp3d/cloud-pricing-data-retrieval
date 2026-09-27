# Phase 1 Contracts: Parquet Partition Success Markers

There are two contracts: the **on-disk marker contract** that downstream consumers rely on, and the **internal Python interfaces** that the transform and tests are written against.

## 1. Consumer contract (on disk)

A downstream consumer that wants table `T` for date `D`:

1. Checks whether `<parquet_root>/T/snapshot_date=D/_SUCCESS` exists.
2. If it exists, all configured regions' data for `T`/`D` is fully written and safe to read. Reading `T/snapshot_date=D/` returns the complete set of rows.
3. If it doesn't exist, the partition is still being written, is partially failed, or had no data. Don't read it as complete.

Guarantees:
- `_SUCCESS` is empty and sits only at the `snapshot_date=` level.
- `_REGION_COMPLETE` files inside `region=` folders are internal. Consumers must not depend on them.
- Files and folders whose names start with `_` or `.` are not data. Readers skip them (the pyarrow/Spark default).
- Partitions for other dates are never modified by a run for `D`.

## 2. `src/partition_markers.py` (new)

```python
SUCCESS_MARKER: str = "_SUCCESS"
REGION_COMPLETE_MARKER: str = "_REGION_COMPLETE"

class FinalizeStatus(str, Enum):
    WRITTEN = "WRITTEN"
    INCOMPLETE = "INCOMPLETE"
    NO_DATA = "NO_DATA"

@dataclass
class FinalizeOutcome:
    status: FinalizeStatus
    missing_regions: List[str]          # non-empty only when INCOMPLETE

@contextmanager
def partition_lock(parquet_root: str, table: str, snapshot_date: str) -> Iterator[None]:
    """Exclusive, blocking, cross-process lock (fcntl.flock) for one (table, date).
    Lock file: <parquet_root>/.locks/<table>/snapshot_date=<date>.lock."""

@contextmanager
def region_run_lock(parquet_root: str, snapshot_date: str, region: str) -> Iterator[None]:
    """Exclusive, blocking flock for one (date, region), held across the whole transform write loop (FR-017).
    Lock file: <parquet_root>/.locks/_regions/snapshot_date=<date>/region=<region>.lock."""

def invalidate_region(parquet_root: str, table: str, snapshot_date: str, region: str) -> None:
    """Under partition_lock: remove <table>/snapshot_date=<date>/_SUCCESS, then remove
    region=<region>/_REGION_COMPLETE, then remove region=<region>/*.parquet.
    Missing files are not errors. Touches only this (table, date, region)."""

def mark_region_complete(parquet_root: str, table: str, snapshot_date: str, region: str) -> str:
    """Create an empty region=<region>/_REGION_COMPLETE, creating the region folder if
    needed (0-row case, FR-015). Atomic (temp file + os.replace). Returns the path."""

def finalize_partition(
    parquet_root: str, table: str, snapshot_date: str, expected_regions: Sequence[str]
) -> FinalizeOutcome:
    """Under partition_lock: apply the completion rule (research.md Decision 5).
    Writes <table>/snapshot_date=<date>/_SUCCESS atomically only when the result is WRITTEN.
    Idempotent: re-running it on a READY partition returns WRITTEN and leaves the partition as it was."""
```

Errors: all functions raise `OSError` on filesystem failure. Callers turn these into `marker_errors`.

## 3. `src/pricing_parquet_transformations.py` (changed)

```python
@dataclass
class TransformRequest:
    json_files: List[str]
    parquet_root: str
    region: str
    snapshot_date: str = ""
    log_callback: Optional[Callable[[str], None]] = None
    expected_regions: Optional[List[str]] = None   # NEW — None ⇒ aws_regions.resolve_regions()

@dataclass
class TransformResult:
    parquet_root: str
    snapshot_date: str = ""
    tables_written: List[str]
    tables_skipped: List[str]
    errors: List[str]
    success: bool = False
    marker_outcomes: Dict[str, str] = {}   # NEW — table → "WRITTEN" | "INCOMPLETE (missing: …)" | "NO_DATA" | "SKIPPED (…)"
    marker_errors: List[str] = []          # NEW — FR-010
    parse_failed_files: List[str] = []     # NEW — FR-016
```

What `transform_pricing_to_parquet` now does, holding `region_run_lock(parquet_root, snapshot_date, region)` for the whole loop (JSON parsing stays outside it), for each of the 5 tables in order:
1. `invalidate_region(...)`, **always**, even if the table has 0 rows now (removes stale data, Decision 6).
2. If there are rows, write `part-0.parquet` (unchanged).
3. If the write succeeded (or there were 0 rows) **and** no input file failed to parse: `mark_region_complete(...)`, then `finalize_partition(...)`. Otherwise record the outcome `SKIPPED`.
4. Any `OSError` in steps 1, 3 or 4 is appended to `marker_errors`.

`result.success` keeps its current meaning (at least one table written). Existing callers and tests are unaffected apart from the new fields.

## 4. `src/dagster_app/assets/pricing_assets.py::transform_to_parquet` (changed)

```python
@op
def transform_to_parquet(
    context: OpExecutionContext,
    raw_dir: str,
    config: TransformConfig,
    pricing_regions: PricingRegionsResource,     # NEW — injected resource
) -> str:
```
- Passes `expected_regions=pricing_regions.get_regions()` into the `TransformRequest`.
- Logs `marker_outcomes` one line per table.
- Raises `RuntimeError` if `marker_errors` is non-empty (FR-010), after the existing "no output tables" check.
- Raises `RuntimeError` if `result.parse_failed_files` is non-empty (FR-016), after logging marker outcomes.

No change to the job, schedule, sensor, or `Definitions`. The `pricing_regions` resource is already registered.

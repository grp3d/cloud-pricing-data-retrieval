# Phase 0 Research: Parquet Partition Success Markers

All open questions from the spec were resolved in `/speckit-clarify` (Session 2026-09-26). This document records the design decisions that remained for planning, most importantly how concurrent region runs coordinate on a shared snapshot-date partition.

## Decision 1: Who runs the completion check — every region run, at the end of its own transform

- **Decision**: Every `transform_to_parquet` op, after it has written its own region's data and `_REGION_COMPLETE` for a table, runs the completion check for that table and snapshot date. Whichever region run finishes last therefore writes `_SUCCESS`. There is no separate "finalize" job or op.
- **Rationale**: FR-004 (clarified) requires the check to work for scheduled, sensor, and manual single-region re-runs alike. If the check lives inside the per-region op, a manual re-run of the one failed region is automatically the run that completes the date. No extra job, sensor, or run-status polling is needed, and the existing one-job/N-runs fan-out (`build_region_run_requests`) stays unchanged.
- **Alternatives considered**:
  - *A separate finalize job launched after all region runs*: needs a way to know when N independent runs have all ended (a run-status sensor), and a manual re-run would also have to trigger it. More moving parts, and a gap if the sensor is off.
  - *A run-status sensor that checks after every region run*: works, but splits the marker logic between the transform and a sensor and adds sensor latency. Rejected for simplicity.

## Decision 2: Concurrency control — an OS-level exclusive lock per (table, snapshot_date)

- **Decision**: Two critical sections are serialized under an exclusive `fcntl.flock` lock, one lock file per table and snapshot date, stored at `<parquet_root>/.locks/<table>/snapshot_date=<date>.lock`:
  1. **Invalidate** (before a region rewrites a table): remove `snapshot_date=<date>/_SUCCESS`, remove this region's `_REGION_COMPLETE`, and remove this region's existing data files.
  2. **Finalize** (after a region wrote the table and its `_REGION_COMPLETE`): check every configured region's `_REGION_COMPLETE`, and write `_SUCCESS` if the partition is complete (see Decision 5).
  Writing the data files themselves happens **outside** the lock, so regions still write in parallel.
- **Rationale**: Without a lock there is a real false-positive race. Region B's finalize sees region A's old `_REGION_COMPLETE`; then region A (a re-run) deletes its marker and `_SUCCESS`; then B writes `_SUCCESS` over a partition A is about to rewrite. That breaks SC-002. Serializing invalidate and finalize removes that interleaving. The "two regions finish together, neither writes the marker" worry is also covered: each region writes its own `_REGION_COMPLETE` *before* taking the lock to finalize, so whichever finalize runs second always sees both markers. `flock` locks are released automatically by the OS if the process dies, so a crashed run can never leave a stale lock that blocks later runs.
- **Region-run lock (FR-017)**: a second, coarser `flock` lock at `<parquet_root>/.locks/_regions/snapshot_date=<D>/region=<R>.lock` is held for the region's whole per-table loop (remove → write → mark → check), so two runs of the *same* region and date can never write `part-0.parquet` at the same time. Lock order is always region lock (outer) → partition lock (inner), and the inner lock never takes the outer one, so they can't deadlock. Different regions still run in parallel.
- **Where the lock file lives**: under a hidden `.locks/` folder at the parquet root, **not** inside any `snapshot_date=` folder. This keeps FR-003 (never touch other partitions) and FR-009 (no extra files in data folders) clean. Readers already skip names starting with `.` and `_`.
- **Alternatives considered**:
  - *`O_CREAT | O_EXCL` lock files*: a crash leaves a stale lock that needs manual cleanup. Rejected.
  - *Dagster concurrency pools/limits*: they serialize whole runs, not just the marker step, so regions would lose their parallelism. They also don't cover direct library use. Rejected.
  - *No lock, just atomic file operations*: doesn't prevent the re-run race described above. Rejected.
- **Constraint**: `flock` is reliable on local filesystems. Using it on network filesystems (NFS/SMB) is not guaranteed; that's already out of scope per the spec's Assumptions (local/mounted filesystem only).

## Decision 3: What "configured regions" means at check time

- **Decision**: The `transform_to_parquet` op gets `PricingRegionsResource` injected and passes `pricing_regions.get_regions()` into the transform as `expected_regions`. When the library is called without it (`expected_regions=None`), it falls back to `aws_regions.resolve_regions()`, the same default the resource uses.
- **Rationale**: Clarification Q1 said: the current configured list, read when the check runs. The resource is already the single source of truth for the schedule and sensor fan-out.
- **Edge**: If the region running the transform is *not* in the configured list (for example an ad-hoc run for an extra region), it still writes its data and `_REGION_COMPLETE`. That region is simply not required for completion, and the check still runs over the configured list.

## Decision 4: When a region counts as successful for a table

- **Decision**: A region writes `_REGION_COMPLETE` for a table only if **both**:
  1. that table's data write succeeded (or the table had zero rows for this region, in which case the folder holds only `_REGION_COMPLETE` per FR-015), **and**
  2. **every** input JSON file for the region parsed successfully.
  If any input file failed to parse, the region writes its data as today but writes **no** `_REGION_COMPLETE` for any table, and the Dagster op **raises** (the run fails) after logging which files failed. So a missing `_SUCCESS` always matches a visible failed run (FR-016).
- **Rationale**: A skipped service file means the region's data is silently incomplete. Marking it done would break the meaning of `_SUCCESS`. A re-run downloads the files again and gives the region another chance to succeed.
- **Alternatives considered**: Ignore parse errors, as the transform does today (it warns and carries on). Rejected, because it would allow `_SUCCESS` on incomplete data.

## Decision 5: Completion rule evaluated inside finalize

For table `T` and date `D`, with `expected = expected_regions`:

1. For every `r` in `expected`: `T/snapshot_date=D/region=r/_REGION_COMPLETE` exists. If any is missing, the result is **INCOMPLETE** (log which regions are missing), no `_SUCCESS`.
2. At least one `r` in `expected` has at least one `*.parquet` data file in its region folder. Otherwise the result is **NO_DATA** (FR-007), no `_SUCCESS`.
3. Otherwise, write `_SUCCESS`. The result is **WRITTEN**.

Region folders for regions that are no longer configured are ignored (spec edge case: region list changes).

## Decision 6: Stale data files on rewrite

- **Decision**: Invalidate (Decision 2) deletes the region's existing data files (`*.parquet`) for that table and date before writing new ones.
- **Rationale**: Today the writer overwrites `part-0.parquet` only when the table has rows. If a re-run now produces zero rows for a table that previously had data, the old file would stay behind and be read as current data. Deleting first makes the region folder exactly reflect the latest successful run. This only affects the current date's region folder, which is allowed under FR-003.

## Decision 7: Marker file creation

- **Decision**: Markers are created as empty files. `_SUCCESS` is written via a temp file in the same folder (`.<name>.tmp`) plus `os.replace`, so it appears atomically. `_REGION_COMPLETE` is created the same way.
- **Rationale**: FR-008 (empty file). The atomic rename means a reader never sees a half-created marker, which is trivial for an empty file anyway but costs nothing.

## Decision 8: Error surfacing

- **Decision**: Any failure while invalidating, writing `_REGION_COMPLETE`, or finalizing is recorded in a new `TransformResult.marker_errors` list. The Dagster op logs each one and **raises** (fails the run) if the list is non-empty, after logging the per-table marker outcomes.
- **Rationale**: FR-010: a marker failure must be reported as an error for that run, not ignored. Failing the run makes it visible in Dagster and makes it clear a re-run is needed.
- **Per-table outcome logging**: the op logs one line per table: `WRITTEN`, `INCOMPLETE (missing: …)`, `NO_DATA`, or `SKIPPED (region not complete)`.

## Decision 9: Test approach

- **Decision**: pytest unit tests for the new module using `tmp_path` (no Dagster needed). Concurrency is tested with `multiprocessing` (real separate processes, so `flock` is exercised). An integration test runs `transform_pricing_to_parquet` once per region against a shared `parquet_root` and checks the marker lifecycle end to end. This matches the existing `tests/unit` and `tests/integration` layout.
